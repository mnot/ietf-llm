"""IESG ballot positions for drafts under WG consideration.

When a draft progresses to the IESG, each Area Director casts a ballot
position: `yes`, `noobj` (No Objection), `discuss` (holds publication
pending resolution), `abstain`, or `recuse`. DISCUSS positions and
their text are load-bearing for "what's at the IESG"-shaped questions
— and previously invisible to ietf-llm consumers, who had to guess at
chair characterisations from list traffic alone.

Two slugs beyond that set matter, and both were missing until an
invented one (`noupcoming`, which Datatracker does not emit) stood in
for the first. `norecord` is what the API records for a balloter
holding *no* position — never posted, or posted and later cleared —
and it is common; rendered as a position it inflates the tally and
implies a balloter weighed in when they did not. `block` / `concern` /
`moretime` / `notready` are the IAB and IRSG ballots, which an RG
corpus reaches through this same endpoint. The full vocabulary is
`/api/v1/name/ballotpositionname/`.

Scoping. Long-running WGs have decades of ballot history, almost all
of it irrelevant to a current reader. We clamp gathering to drafts
that have had **at least one ballot position event** in the
`--months` window (typically 12). For each such draft we then fetch
the *full* current ballot — every AD's latest position — even where
the position was placed before the window. A 13-month-old standing
DISCUSS is still active and must show up.

One ballot per document. A document that was re-balloted (every recharter,
a draft sent back to the IESG) has several `BallotDocEvent`s, and each
position event names the one it belongs to. Only the newest ballot is
read; folding all of them together would present positions from closed
ballots as current. Within it, a balloter who is no longer on the IESG
(or IRSG) holds nothing, so such positions are listed apart and left out
of the tally. The charter documents the scoping query also sweeps in get
the same treatment and are headed as charter ballots.

Storage. One markdown file per in-scope draft at
`ballots/<doc-name>.md`. Header carries the revision balloted on, the
ballot-opened date, and a tally line. Blocking positions render first
(with their full filed text), then the others ordered by AD name.

Events. Each in-window position change emits an `Event` with
`kind="ballot"` for the chronological timeline digest. The link
field points at the per-draft ballot file so the consumer can pivot
to the full ballot in one read.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...atomicio import write_if_changed
from ...digest.events import Event
from ...log import LogLevel, Verbosity, log
from ...paths import ballot_path, ballots_dir
from .datatracker import (
    _get_json,
    fetch_person_names,
)

_API_BASE = "https://datatracker.ietf.org/api/v1"

#: Who can hold a position on a ballot today: the sitting ADs, and the IRSG
#: for the research-group ballots that share this endpoint.
_BALLOTER_QUERIES = (
    "/group/role/?group__acronym=iesg&name=ad",
    "/group/role/?group__acronym=irsg",
)

# Datatracker position slug → human label and order, the full vocabulary of
# `/api/v1/name/ballotpositionname/`. Order matters for rendering: blocking
# positions come first because they're load-bearing (they hold publication);
# other positions follow. `block` / `concern` / `moretime` / `notready` are
# the IAB and IRSG ballots, which an RG corpus reaches through this same
# endpoint — omitting them left their slug to leak through the `.upper()`
# fallback as a position label.
_POSITION_LABELS = {
    "discuss": "DISCUSS",
    "block": "Block",
    "concern": "Concern",
    "yes": "Yes",
    "noobj": "No Objection",
    "notready": "Not Ready",
    "moretime": "Need More Time",
    "abstain": "Abstain",
    "recuse": "Recuse",
    "norecord": "No Record",
}
# Sort within a ballot rendering. Lower = appears earlier.
_POSITION_ORDER = {
    "discuss": 0,
    "block": 1,
    "concern": 2,
    "yes": 3,
    "noobj": 4,
    "notready": 5,
    "moretime": 6,
    "abstain": 7,
    "recuse": 8,
    "norecord": 9,
}

#: Positions that hold publication until cleared. They render first, with the
#: text the balloter filed — which is the whole reason the section exists, and
#: which the "other positions" rendering drops.
_BLOCKING = ("discuss", "block", "concern")

#: Not a position on the document: Datatracker records `norecord` for a
#: balloter who has taken none, either never having posted one or having
#: cleared it. Dropped rather than rendered — counted as a position it both
#: inflates the tally and implies a balloter weighed in when they did not.
_NO_POSITION = "norecord"

_DOC_URL_RE = re.compile(r"/api/v1/doc/document/([^/]+)/?$")
_POS_URL_RE = re.compile(r"/api/v1/name/ballotpositionname/([^/]+)/?$")
_BALLOT_URL_RE = re.compile(r"/api/v1/doc/ballotdocevent/(\d+)/?$")
_PERSON_URL_RE = re.compile(r"/api/v1/person/person/(\d+)/?$")


@dataclass
class Position:
    """One AD's current ballot position on a draft."""

    person_url: str
    name: str  # canonical AD name from Datatracker
    pos_slug: str  # 'discuss', 'noobj', 'yes', 'abstain', 'recuse'
    pos_label: str  # human label ('DISCUSS', 'No Objection', …)
    when: datetime  # UTC of the LATEST event for this AD on this doc
    rev: Optional[str]  # revision the position was placed against
    comment: str  # general comment (may be empty)
    discuss: str  # DISCUSS text (only set on DISCUSS positions)
    # False for a balloter no longer on the IESG / IRSG: kept for the record,
    # but neither tallied nor treated as blocking.
    current: bool = True


@dataclass
class Ballot:
    """A draft's current ballot snapshot — one Position per AD."""

    doc_name: str  # 'draft-ietf-tls-rfc8446bis'
    positions: List[Position] = field(default_factory=list)
    # Earliest position event we saw, useful as "ballot opened" hint.
    first_event: Optional[datetime] = None
    # Latest position event in the --months window. Used for chronological
    # bucketing in the timeline.
    latest_in_window: Optional[datetime] = None
    # Revision the most-recent position was placed against (best
    # available signal of "current revision under consideration").
    latest_rev: Optional[str] = None


# --- API helpers -----------------------------------------------------------


def _cutoff(months: int) -> datetime:
    """tz-aware UTC cutoff `months` months before now, at midnight. Mirrors
    `datatracker_history._cutoff` (same 30-day approximation) but rounded to
    the day: the cutoff is part of the scoping URL, and one that changed every
    second could never revalidate against the ETag cache."""
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=30 * months)


def _parse_dt_time(value: Any) -> Optional[datetime]:
    """Parse Datatracker's `time` field — ISO 8601 with a trailing Z."""
    if not isinstance(value, str) or not value:
        return None
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _slug_from_doc_url(url: str) -> Optional[str]:
    match = _DOC_URL_RE.search(url or "")
    return match.group(1) if match else None


def _slug_from_pos_url(url: str) -> Optional[str]:
    match = _POS_URL_RE.search(url or "")
    return match.group(1) if match else None


def _fetch_person_name(person_url: str, cache: Dict[str, str]) -> str:
    """Resolve a person URL to a display name. Cached per call so
    repeat ADs across positions don't re-fetch."""
    if person_url in cache:
        return cache[person_url]
    body = _get_json(person_url) if person_url else None
    name = ""
    if body:
        name = body.get("name") or body.get("ascii") or ""
    cache[person_url] = name
    return name


# --- Public API -----------------------------------------------------------


#: Docs per batched ballot-history query. `doc__name__in` takes a comma list;
#: keep chunks modest so the URL stays a sane length.
_BALLOT_DOC_CHUNK = 40


def _fetch_current_balloters() -> Optional[frozenset[str]]:
    """Person ids of everyone who can hold a position on a ballot today, or
    None when that cannot be established (a failed fetch, or an empty answer).
    The caller must not guess: "nobody is current" and "everybody is current"
    are both wrong."""
    ids: set[str] = set()
    for query in _BALLOTER_QUERIES:
        path: Optional[str] = f"{_API_BASE}{query}&limit=200"
        while path:
            body = _get_json(path)
            if not body:
                return None
            for obj in body.get("objects") or []:
                match = _PERSON_URL_RE.search(obj.get("person") or "")
                if match:
                    ids.add(match.group(1))
            path = (body.get("meta") or {}).get("next") or None
    return frozenset(ids) or None


def _person_id(url: str) -> str:
    match = _PERSON_URL_RE.search(url or "")
    return match.group(1) if match else ""


def _fetch_ballot_bodies(
    doc_names: List[str],
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Fetch the full position-event history for many docs at once, grouped by
    doc name.

    Collapses what used to be one `doc__name=<doc>` request per in-scope doc
    into ceil(len / chunk) `doc__name__in=<chunk>` requests, paging each chunk
    through `meta.next` so a chunk whose combined history exceeds one page is
    never truncated. This is the bulk of a balloted WG's per-gather datatracker
    chatter, so the saving is direct upstream load relief.

    Returns None if any page failed: a missing page would silently turn a
    document's ballot into a partial one, or drop it."""
    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in doc_names}
    for start in range(0, len(doc_names), _BALLOT_DOC_CHUNK):
        chunk = doc_names[start : start + _BALLOT_DOC_CHUNK]
        path: Optional[str] = (
            f"{_API_BASE}/doc/ballotpositiondocevent/"
            f"?doc__name__in={','.join(chunk)}"
            "&limit=500"
        )
        while path:
            body = _get_json(path)
            if not body:
                return None
            for obj in body.get("objects") or []:
                slug = _slug_from_doc_url(obj.get("doc") or "")
                if slug in grouped:
                    grouped[slug].append(obj)
            path = (body.get("meta") or {}).get("next") or None
    return grouped


def fetch_ballots(
    wg: str, months: int, verbose: Verbosity = Verbosity.STATUS
) -> Optional[List[Ballot]]:
    """Return ballot snapshots for every WG draft active in the
    `--months` window, or None if Datatracker could not be read.

    None is not `[]`: an empty list means "no ballot activity" and lets the
    caller clear the stale ballot files, whereas an unreachable Datatracker
    says nothing about them.

    Scoping rule: a draft is in-scope iff at least one ballot position
    event for it happened in the window. For each in-scope draft we
    fetch the full ballot history (no time filter), keep its newest
    ballot, and collapse that to one Position per balloter via "latest
    event wins".
    """
    cutoff = _cutoff(months)
    cutoff_param = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    # Page through `meta.next` (like `_fetch_ballot_bodies`): a balloted WG can
    # have more than 500 in-window position events, and a single capped request
    # would drop the overflow — under-scoping which drafts are considered.
    path: Optional[str] = (
        f"{_API_BASE}/doc/ballotpositiondocevent/"
        f"?doc__group__acronym={wg}"
        f"&time__gte={cutoff_param}"
        "&limit=500"
    )
    in_window: List[Dict[str, Any]] = []
    while path:
        body = _get_json(path)
        if not body:
            log(
                f"Could not read ballot positions for {wg} from Datatracker; "
                "leaving existing ballot files as they are.",
                verbose,
                level=LogLevel.WARN,
            )
            return None
        in_window.extend(body.get("objects") or [])
        path = (body.get("meta") or {}).get("next") or None
    if not in_window:
        log(
            f"No ballot data for {wg} from Datatracker.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []
    # Distinct doc names with any in-window position event.
    doc_names: List[str] = []
    seen: set[str] = set()
    for obj in in_window:
        slug = _slug_from_doc_url(obj.get("doc") or "")
        if slug and slug not in seen:
            seen.add(slug)
            doc_names.append(slug)
    if not doc_names:
        log(
            f"No drafts with ballot activity in the last {months} months.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []
    log(
        f"Fetching IESG ballots for {len(doc_names)} draft(s) "
        f"with in-window activity: {', '.join(doc_names)}",
        verbose,
        level=LogLevel.STATUS,
    )
    # Pull every in-scope doc's full ballot history in batched
    # `doc__name__in` queries (not one request per doc), then collect the
    # distinct balloters across all of them.
    grouped = _fetch_ballot_bodies(doc_names)
    if grouped is None:
        log(
            f"Could not read the ballot history for {wg} from Datatracker; "
            "leaving existing ballot files as they are.",
            verbose,
            level=LogLevel.WARN,
        )
        return None
    # Without the role lists every departed balloter would count as current,
    # and a good file would be rewritten with their blocking positions.
    current = _fetch_current_balloters()
    if current is None:
        log(
            f"Could not establish the current balloters for {wg}; "
            "leaving existing ballot files as they are.",
            verbose,
            level=LogLevel.WARN,
        )
        return None
    bodies: List[Tuple[str, Dict[str, Any]]] = []
    balloters: set[str] = set()
    for doc_name in doc_names:
        objs = grouped.get(doc_name)
        if not objs:
            continue
        bodies.append((doc_name, {"objects": objs}))
        for obj in objs:
            balloter = obj.get("balloter") or obj.get("by") or ""
            if balloter:
                balloters.add(balloter)

    # One batched query resolves every balloter (the same ~15 ADs recur
    # across docs) instead of one GET per balloter.
    person_cache: Dict[str, str] = fetch_person_names(balloters)

    ballots: List[Ballot] = []
    for doc_name, body in bodies:
        ballot = _build_ballot(doc_name, body, cutoff, person_cache, current)
        if ballot is None or not ballot.positions:
            continue
        ballots.append(ballot)
    return ballots


def _build_ballot(
    doc_name: str,
    body: Dict[str, Any],
    cutoff: datetime,
    person_cache: Dict[str, str],
    current: Optional[frozenset[str]] = None,
) -> Optional[Ballot]:
    """Collapse one doc's position events to its newest ballot (latest
    event per balloter wins). `person_cache` is pre-resolved by the
    caller; any balloter it misses falls back to an individual lookup.
    `current` is the set of person ids still entitled to ballot; None
    marks no position former.

    "Newest" is the ballot with the most recent position event. A ballot
    just opened, before anyone has voted, has no events yet, so the previous
    ballot stands in until its first position arrives."""
    events: List[Tuple[str, Dict[str, Any], datetime]] = []
    first_event: Optional[datetime] = None
    latest_in_window: Optional[datetime] = None
    latest_rev: Optional[str] = None
    for balloter, obj, when in _newest_ballot_events(body["objects"]):
        events.append((balloter, obj, when))
        if first_event is None or when < first_event:
            first_event = when
        if when >= cutoff:
            if latest_in_window is None or when > latest_in_window:
                latest_in_window = when
                rev = obj.get("rev")
                if isinstance(rev, str) and rev:
                    latest_rev = rev
    # Latest-event-per-balloter: walk events newest-first.
    events.sort(key=lambda triple: triple[2], reverse=True)
    seen: set[str] = set()
    positions: List[Position] = []
    for balloter, obj, when in events:
        if balloter in seen:
            continue
        seen.add(balloter)
        pos_slug = _slug_from_pos_url(obj.get("pos") or "") or _NO_POSITION
        # `norecord` / blank: the balloter holds no position on this ballot.
        # Skip — there's nothing to render.
        if pos_slug == _NO_POSITION:
            continue
        name = _fetch_person_name(balloter, person_cache)
        if not name:
            # Fall back to the person ID; better than dropping the
            # position entirely.
            match = _PERSON_URL_RE.search(balloter)
            name = f"(person {match.group(1)})" if match else balloter
        rev = obj.get("rev")
        positions.append(
            Position(
                current=current is None or _person_id(balloter) in current,
                person_url=balloter,
                name=name,
                pos_slug=pos_slug,
                pos_label=_POSITION_LABELS.get(pos_slug, pos_slug.upper()),
                when=when,
                rev=rev if isinstance(rev, str) and rev else None,
                comment=str(obj.get("comment") or "").strip(),
                discuss=str(obj.get("discuss") or "").strip(),
            )
        )
    return Ballot(
        doc_name=doc_name,
        positions=positions,
        first_event=first_event,
        latest_in_window=latest_in_window,
        latest_rev=latest_rev,
    )


def _newest_ballot_events(
    objects: List[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any], datetime]]:
    """The `(balloter, event, time)` triples belonging to the document's
    newest ballot. Events naming no ballot are treated as one ballot of
    their own."""
    by_ballot: Dict[str, List[Tuple[str, Dict[str, Any], datetime]]] = {}
    for obj in objects:
        when = _parse_dt_time(obj.get("time"))
        balloter = obj.get("balloter") or obj.get("by") or ""
        if when is None or not balloter:
            continue
        by_ballot.setdefault(str(obj.get("ballot") or ""), []).append(
            (balloter, obj, when)
        )
    if not by_ballot:
        return []

    def recency(ballot_url: str) -> Tuple[datetime, int]:
        # Ballot ids only ever grow, so they break a tie between two ballots
        # whose last events share a timestamp.
        match = _BALLOT_URL_RE.search(ballot_url)
        return (
            max(when for _, _, when in by_ballot[ballot_url]),
            int(match.group(1)) if match else 0,
        )

    return by_ballot[max(by_ballot, key=recency)]


# --- Rendering ------------------------------------------------------------


def render_ballot(ballot: Ballot) -> str:
    """Render one Ballot as markdown for `ballots/<doc-name>.md`.

    Blocking positions appear first (with the full filed text inline);
    other positions follow in their canonical order. The tally line
    in the header lets a consumer see the shape of the ballot at a
    glance without scrolling.
    """
    out: List[str] = []
    kind = "charter ballot" if ballot.doc_name.startswith("charter-") else "ballot"
    out.append(f"# IESG {kind}: `{ballot.doc_name}`\n")
    out.append(
        f"**Document:** "
        f"[{ballot.doc_name}]"
        f"(https://datatracker.ietf.org/doc/{ballot.doc_name}/)  "
    )
    if ballot.latest_rev:
        out.append(f"**Latest revision balloted:** -{ballot.latest_rev}  ")
    if ballot.first_event:
        out.append(
            f"**Earliest ballot event:** "
            f"{ballot.first_event.strftime('%Y-%m-%d')}  "
        )
    standing = [p for p in ballot.positions if p.current]
    former = [p for p in ballot.positions if not p.current]
    counts: Dict[str, int] = {}
    for pos in standing:
        counts[pos.pos_slug] = counts.get(pos.pos_slug, 0) + 1
    tally_bits: List[str] = []
    for slug in sorted(counts, key=lambda s: _POSITION_ORDER.get(s, 99)):
        label = _POSITION_LABELS.get(slug, slug)
        tally_bits.append(f"{counts[slug]} {label}")
    if tally_bits:
        out.append(f"**Tally:** {', '.join(tally_bits)}")
    elif former:
        out.append(
            "**No current balloter holds a position:** every position below "
            "is from a former balloter, so this ballot is most likely closed."
        )
    out.append("")
    blocking = [p for p in standing if p.pos_slug in _BLOCKING]
    other = [p for p in standing if p.pos_slug not in _BLOCKING]
    other.sort(key=lambda p: (_POSITION_ORDER.get(p.pos_slug, 99), p.name))
    if blocking:
        # Headed by the position kind actually present, so an IESG ballot
        # still says "DISCUSS positions" — what a reader (and the `**Tally:**`
        # scan in `digest.overview`) expects — while an IRSG Block is not
        # filed under a heading naming a position it isn't.
        kinds = sorted({p.pos_label for p in blocking})
        out.append(f"## {' / '.join(kinds)} positions\n")
        out.append(
            "_A blocking position holds publication until resolved with the "
            "responsible AD. Text below is the position body as filed; "
            "later list / chair discussion may have addressed it._\n"
        )
        for pos in sorted(blocking, key=lambda p: (p.pos_label, p.name)):
            out.append(
                f"### {pos.name} — {pos.pos_label} — "
                f"{pos.when.strftime('%Y-%m-%d')}"
                + (f" (on -{pos.rev})" if pos.rev else "")
            )
            out.append("")
            if pos.discuss:
                out.append(pos.discuss)
                out.append("")
            if pos.comment:
                out.append("_Additional comment:_")
                out.append("")
                out.append(pos.comment)
                out.append("")
    if other:
        out.append("## Other positions\n")
        for pos in other:
            out.append(
                f"### {pos.name} — {pos.pos_label} — "
                f"{pos.when.strftime('%Y-%m-%d')}"
                + (f" (on -{pos.rev})" if pos.rev else "")
            )
            out.append("")
            if pos.comment:
                out.append(pos.comment)
                out.append("")
    if former:
        # Listed so the file is a full record, but apart from the sections a
        # reader scans for what is holding the document: these seats are empty.
        out.append("## Former balloters (not counted)\n")
        out.append(
            "_Positions from people no longer on the IESG / IRSG. They no "
            "longer hold or block anything and are left out of the tally._\n"
        )
        for pos in sorted(former, key=lambda p: p.name):
            out.append(
                f"- {pos.name} (former balloter) — {pos.pos_label} — "
                f"{pos.when.strftime('%Y-%m-%d')}"
                + (f" (on -{pos.rev})" if pos.rev else "")
            )
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_ballot_files(
    cache_dir: str,
    ballots: List[Ballot],
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Write per-draft ballot files to `<cache_dir>/ballots/`. Returns
    the list of paths written (relative to cache_dir).
    """
    out_dir = ballots_dir(cache_dir)
    if not ballots and not os.path.isdir(out_dir):
        return []
    os.makedirs(out_dir, exist_ok=True)

    # Write-if-changed so a byte-identical ballot isn't needlessly
    # rewritten. `expected` drives orphan cleanup —
    # drafts that fall out of the window must have their stale ballot
    # files removed (even when `ballots` is now empty), or they'd
    # linger as misleading current state.
    all_paths: List[str] = []
    changed = 0
    expected: set[str] = set()
    for ballot in ballots:
        path = ballot_path(cache_dir, ballot.doc_name)
        expected.add(os.path.basename(path))
        all_paths.append(path)
        if write_if_changed(path, render_ballot(ballot)):
            changed += 1

    removed = 0
    for name in os.listdir(out_dir):
        if name.endswith(".md") and name not in expected:
            try:
                os.remove(os.path.join(out_dir, name))
                removed += 1
            except OSError:
                pass

    if all_paths or removed:
        log(
            f"IESG ballot files: {len(all_paths)} current "
            f"({changed} written / changed, {removed} removed)",
            verbose,
            level=LogLevel.STATUS,
        )
    return all_paths


def ballot_events(
    ballots: List[Ballot],
    cache_dir: str,
    cutoff: datetime,
) -> List[Event]:
    """Build timeline Event records for each in-window ballot
    position. One Event per (doc, AD, latest-in-window position).

    Each event's `link` is the relative ballot file path so the
    timeline render can offer a one-click pivot to the full ballot.
    """
    events: List[Event] = []
    for ballot in ballots:
        link = os.path.relpath(
            ballot_path(cache_dir, ballot.doc_name),
            cache_dir,
        )
        for pos in ballot.positions:
            # Render in-window position events only — the historical
            # tail belongs in the ballot file, not the timeline.
            if pos.when < cutoff:
                continue
            title = f"`{ballot.doc_name}`: {pos.name} → {pos.pos_label}"
            events.append(
                Event(
                    when=pos.when,
                    kind="ballot",
                    title=title,
                    detail=None,
                    link=link,
                )
            )
    return events
