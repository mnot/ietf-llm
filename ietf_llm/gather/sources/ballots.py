"""IESG ballot positions for drafts under WG consideration.

When a draft progresses to the IESG, each Area Director casts a ballot
position: `yes`, `noobj` (No Objection), `discuss` (holds publication
pending resolution), `abstain`, or `recuse`. DISCUSS positions and
their text are load-bearing for "what's at the IESG"-shaped questions
— and previously invisible to ietf-llm consumers, who had to guess at
chair characterisations from list traffic alone.

Scoping. Long-running WGs have decades of ballot history, almost all
of it irrelevant to a current reader. We clamp gathering to drafts
that have had **at least one ballot position event** in the
`--months` window (typically 12). For each such draft we then fetch
the *full* current ballot — every AD's latest position — even where
the position was placed before the window. A 13-month-old standing
DISCUSS is still active and must show up.

Storage. One markdown file per in-scope draft at
`ballots/<doc-name>.md`. Header carries the revision balloted on, the
ballot-opened date, and a tally line. DISCUSS positions render first
(with full discuss text), then the other positions ordered by AD name.

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
from ...paths import ballot_path, ballots_dir
from ...utils import LogLevel, Verbosity, log
from .datatracker import (  # pylint: disable=protected-access
    _get_json,
    fetch_person_names,
)

_API_BASE = "https://datatracker.ietf.org/api/v1"

# Datatracker position slug → human label and order. Order matters for
# rendering: DISCUSS positions come first because they're load-bearing
# (they hold publication); other positions follow.
_POSITION_LABELS = {
    "discuss": "DISCUSS",
    "yes": "Yes",
    "noobj": "No Objection",
    "abstain": "Abstain",
    "recuse": "Recuse",
    "noupcoming": "Not Yet Posted",
}
# Sort within a ballot rendering. Lower = appears earlier.
_POSITION_ORDER = {
    "discuss": 0,
    "yes": 1,
    "noobj": 2,
    "abstain": 3,
    "recuse": 4,
    "noupcoming": 5,
}

_DOC_URL_RE = re.compile(r"/api/v1/doc/document/([^/]+)/?$")
_POS_URL_RE = re.compile(r"/api/v1/name/ballotpositionname/([^/]+)/?$")
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
    """tz-aware UTC cutoff `months` months before now. Mirrors
    `datatracker_history._cutoff` — same 30-day approximation."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
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


def _fetch_ballot_bodies(
    doc_names: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch the full position-event history for many docs at once, grouped by
    doc name.

    Collapses what used to be one `doc__name=<doc>` request per in-scope doc
    into ceil(len / chunk) `doc__name__in=<chunk>` requests, paging each chunk
    through `meta.next` so a chunk whose combined history exceeds one page is
    never truncated. This is the bulk of a balloted WG's per-gather datatracker
    chatter, so the saving is direct upstream load relief."""
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
                break
            for obj in body.get("objects") or []:
                slug = _slug_from_doc_url(obj.get("doc") or "")
                if slug in grouped:
                    grouped[slug].append(obj)
            path = (body.get("meta") or {}).get("next") or None
    return grouped


def fetch_ballots(
    wg: str, months: int, verbose: Verbosity = Verbosity.STATUS
) -> List[Ballot]:
    """Return ballot snapshots for every WG draft active in the
    `--months` window.

    Scoping rule: a draft is in-scope iff at least one ballot position
    event for it happened in the window. For each in-scope draft we
    fetch the full ballot history (no time filter) and collapse to
    one Position per AD via "latest event wins".
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
            break
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
        ballot = _build_ballot(doc_name, body, cutoff, person_cache)
        if ballot is None or not ballot.positions:
            continue
        ballots.append(ballot)
    return ballots


def _build_ballot(
    doc_name: str,
    body: Dict[str, Any],
    cutoff: datetime,
    person_cache: Dict[str, str],
) -> Optional[Ballot]:
    """Collapse one doc's position events to the current ballot (latest
    event per balloter wins). `person_cache` is pre-resolved by the
    caller; any balloter it misses falls back to an individual lookup."""
    events: List[Tuple[str, Dict[str, Any], datetime]] = []
    first_event: Optional[datetime] = None
    latest_in_window: Optional[datetime] = None
    latest_rev: Optional[str] = None
    for obj in body["objects"]:
        when = _parse_dt_time(obj.get("time"))
        if when is None:
            continue
        balloter = obj.get("balloter") or obj.get("by") or ""
        if not balloter:
            continue
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
        pos_slug = _slug_from_pos_url(obj.get("pos") or "") or "noupcoming"
        # "noupcoming" / blank: balloter hasn't yet posted a position
        # on this ballot. Skip — there's nothing to render.
        if pos_slug == "noupcoming":
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
                person_url=balloter,
                name=name,
                pos_slug=pos_slug,
                pos_label=_POSITION_LABELS.get(pos_slug, pos_slug.upper()),
                when=when,
                rev=str(rev) if isinstance(rev, str) and rev else None,
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


# --- Rendering ------------------------------------------------------------


def render_ballot(ballot: Ballot) -> str:
    """Render one Ballot as markdown for `ballots/<doc-name>.md`.

    DISCUSS positions appear first (with full discuss text inline);
    other positions follow in their canonical order. The tally line
    in the header lets a consumer see the shape of the ballot at a
    glance without scrolling.
    """
    out: List[str] = []
    out.append(f"# IESG ballot: `{ballot.doc_name}`\n")
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
    counts: Dict[str, int] = {}
    for pos in ballot.positions:
        counts[pos.pos_slug] = counts.get(pos.pos_slug, 0) + 1
    tally_bits: List[str] = []
    for slug in sorted(counts, key=lambda s: _POSITION_ORDER.get(s, 99)):
        label = _POSITION_LABELS.get(slug, slug)
        tally_bits.append(f"{counts[slug]} {label}")
    if tally_bits:
        out.append(f"**Tally:** {', '.join(tally_bits)}")
    out.append("")
    discuss = [p for p in ballot.positions if p.pos_slug == "discuss"]
    other = [p for p in ballot.positions if p.pos_slug != "discuss"]
    other.sort(key=lambda p: (_POSITION_ORDER.get(p.pos_slug, 99), p.name))
    if discuss:
        out.append("## DISCUSS positions\n")
        out.append(
            "_DISCUSS holds publication until resolved with the "
            "responsible AD. Text below is the DISCUSS body as filed; "
            "later list / chair discussion may have addressed it._\n"
        )
        for pos in sorted(discuss, key=lambda p: p.name):
            out.append(
                f"### {pos.name} — DISCUSS — "
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
