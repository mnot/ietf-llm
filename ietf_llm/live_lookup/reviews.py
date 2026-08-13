"""The review and ballot record for one draft, joined and attributed to the
revision each input was cast against.

Datatracker holds the two halves apart: directorate review assignments under
`review/reviewassignment/`, IESG ballot positions under
`doc/ballotpositiondocevent/`. Neither is much use alone at the point it
matters — a document at IESG Evaluation, where the question is not "what did
reviewers say" but *"has anyone looked at the text that is actually in front of
them"*. Answering that needs both halves keyed by revision, and the join is
easy to get wrong by hand: three reviews collapse into one, an assignment that
produced nothing disappears, and the resulting review reads as though the
current text has been examined when it has not.

Two fields carry the weight, and both are dropped by the obvious shortcuts:

  - **`reviewed_rev` / `rev`** — the revision the review or position was cast
    against. Datatracker's own ballot *page* does not show it, so scraping the
    HTML cannot produce this record; the API can.
  - **the assignments that produced no review** — rejected, or assigned and
    never completed. Filtering to completed rows hides a directorate that
    silently returned nothing, which is a fact about the review coverage.

**The filter gotcha.** `reviewassignment` takes
`review_request__doc__name=`. The obvious `?doc=` is *not* an error: it is
silently ignored and the API returns every assignment it has (18,000+),
starting with an unrelated document — which reads as a plausible result and is
not one.

Live, like the rest of this package: reviews land and positions change daily,
and the derived "nothing has been cast against the current revision" line is a
claim about *now*. A gathered snapshot would answer it confidently and wrongly
the day after a review lands. Everything goes through the shared TTL cache, and
the person / team / result-name resolutions are batched (`?…__in=`) so the
whole record costs a handful of requests, most of them shared with any other
lookup in the session.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from ..gather.sources.citations import normalize_draft_name
from .cache import _API_BASE, _DT_BASE, _cached_json, _now_utc

#: Ballot position slug → label, the full vocabulary of
#: `/api/v1/name/ballotpositionname/`. The last four are the IAB / IRSG
#: ballots, which use this same endpoint. Positions render in the order they
#: were cast, so no sort order is needed here.
_POSITION_LABELS = {
    "discuss": "DISCUSS",
    "yes": "Yes",
    "noobj": "No Objection",
    "abstain": "Abstain",
    "recuse": "Recuse",
    "norecord": "No Record",
    "block": "Block",
    "concern": "Concern",
    "moretime": "Need More Time",
    "notready": "Not Ready",
}

#: Slugs that are not a position on the document, and are dropped rather than
#: rendered. `norecord` is what Datatracker records when a balloter has taken
#: no position — either never having posted one or having cleared it — and it
#: is common (thousands of live events), so treating it as a position both
#: inflates the tally and, worse, lets a cleared position stand in for an
#: examination of the revision it names.
_NO_POSITION = {"norecord"}

#: Cap on pages followed when a list endpoint returns `meta.next`. One
#: document's history fits in a page; the cap only stops a runaway.
_MAX_PAGES = 5

_TRAILING_SLUG = re.compile(r"/([^/]+)/?$")


def _slug(uri: Any) -> Optional[str]:
    """The trailing path segment of a Datatracker resource URI.

    Datatracker's name URIs end in the slug itself (`/name/reviewtypename/lc/`
    → `lc`), so a type or state can be read off a row with no extra request.
    """
    if not isinstance(uri, str) or not uri:
        return None
    match = _TRAILING_SLUG.search(uri)
    return match.group(1) if match else None


def _date(value: Any) -> Optional[str]:
    """The `YYYY-MM-DD` of an ISO timestamp, or None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10] if value[4] == "-" and value[7] == "-" else None


def _objects(url: str) -> Tuple[List[Dict[str, Any]], datetime.datetime, bool]:
    """Every object from a list endpoint, following `meta.next`.

    Returns `(objects, fetched_at, answered)`. The flag is load-bearing rather
    than fastidious: `_cached_json` swallows a timeout, a 500 and a decode
    failure alike to `None`, so without it a Datatracker outage is
    indistinguishable from a document nobody has reviewed — and this tool's
    whole payload is a claim of absence. An empty list with `answered=True` is
    a real "none"; with `answered=False` it is "we do not know".
    """
    out: List[Dict[str, Any]] = []
    fetched = _now_utc()
    next_url: Optional[str] = url
    answered = False
    for _ in range(_MAX_PAGES):
        if not next_url:
            break
        body, page_fetched = _cached_json(next_url)
        fetched = page_fetched
        if not body:
            break
        answered = True
        for obj in body.get("objects") or []:
            if isinstance(obj, dict):
                out.append(obj)
        nxt = (body.get("meta") or {}).get("next")
        next_url = f"{_DT_BASE}{nxt}" if isinstance(nxt, str) and nxt else None
    return out, fetched, answered


def _fetch_map(
    path: str, key: str, keys: Iterable[Optional[str]]
) -> Dict[str, Dict[str, Any]]:
    """Batched `?<key>__in=` fetch of many rows, keyed by that field.

    One request instead of N dereferences, which is what keeps this tool's
    cost flat as the reviewer and balloter lists grow. Rows the API omits are
    simply absent from the map, so every caller keeps its own fallback.
    """
    wanted = sorted({str(k) for k in keys if k})
    if not wanted:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(wanted), 100):
        # `+` is a space in a query string, and 297 Datatracker person emails
        # contain one (`prehn.lars+ietf@gmail.com`). Unescaped, the row simply
        # does not come back and the reviewer renders as a raw address — a
        # silent hole in the column this join exists to produce. `,` is the
        # separator, so it stays unescaped.
        chunk = [quote(value, safe="") for value in wanted[start : start + 100]]
        body, _ = _cached_json(
            f"{_API_BASE}/{path}/?{key}__in={','.join(chunk)}&limit=100"
        )
        for obj in (body or {}).get("objects") or []:
            if isinstance(obj, dict) and obj.get(key) is not None:
                out[str(obj[key])] = obj
    return out


def _named(mapping: Dict[str, Dict[str, Any]], uri: Any, want: str) -> Optional[str]:
    """`want` off the row `uri` points at, or None when either is missing."""
    row = mapping.get(_slug(uri) or "")
    value = (row or {}).get(want)
    return value if isinstance(value, str) and value else None


@dataclass
class ReviewRow:
    """One review assignment — completed or not."""

    team: Optional[str]  # directorate acronym ('genart', 'secdir')
    kind: Optional[str]  # review type slug ('lc', 'telechat', 'early')
    reviewer: str
    reviewed_rev: Optional[str]  # revision reviewed; None where none was
    result: Optional[str]  # 'Ready', 'Has issues', … ; None if unfinished
    state: str  # assignment state slug ('completed', 'rejected', …)
    when: Optional[str]  # completed date, else the assignment date

    @property
    def produced_review(self) -> bool:
        return self.state == "completed"


@dataclass
class PositionRow:
    """One balloter's current position."""

    balloter: str
    slug: str  # raw position slug ('yes', 'noobj', 'recuse', …)
    position: str  # 'Yes', 'No Objection', 'DISCUSS', …
    rev: Optional[str]  # revision the position was placed against
    when: Optional[str]
    discuss: bool  # carries DISCUSS text

    @property
    def examined(self) -> bool:
        """Whether this position implies someone read the revision it names.

        A Recuse is a declaration of *not* participating. Counting it as
        input on the text is how an AD who stepped aside comes to stand in
        for one who looked — which would suppress exactly the warning this
        record exists to raise."""
        return self.slug != "recuse"


@dataclass
class ReviewRecord:
    """A draft's review and ballot history, keyed by revision."""

    name: str
    rev: Optional[str]  # current revision
    rev_date: Optional[str]  # when that revision was posted
    reviews: List[ReviewRow] = field(default_factory=list)
    positions: List[PositionRow] = field(default_factory=list)
    #: Halves Datatracker did not answer for ('reviews', 'ballot'). An empty
    #: list beside empty rows means a real absence; a named half means the
    #: rows are unknown, not none — see `_objects`.
    unavailable: List[str] = field(default_factory=list)


def _reviews(doc_name: str) -> Tuple[List[ReviewRow], datetime.datetime, bool]:
    """Every review assignment for `doc_name`, newest first.

    Team and review type come from each row's `review_request` rather than
    from the review document's name: the name only exists once a review has
    been produced, and the unproductive assignments are exactly the rows worth
    keeping.
    """
    rows, fetched, answered = _objects(
        f"{_API_BASE}/review/reviewassignment/"
        f"?review_request__doc__name={quote(doc_name, safe='')}"
        "&format=json&limit=100"
    )
    if not rows:
        return [], fetched, answered

    requests = _fetch_map(
        "review/reviewrequest", "id", (_slug(row.get("review_request")) for row in rows)
    )
    teams = _fetch_map(
        "group/group", "id", (_slug(req.get("team")) for req in requests.values())
    )
    results = _fetch_map(
        "name/reviewresultname", "slug", (_slug(row.get("result")) for row in rows)
    )
    emails = _fetch_map(
        "person/email", "address", (_slug(row.get("reviewer")) for row in rows)
    )
    people = _fetch_map(
        "person/person", "id", (_slug(row.get("person")) for row in emails.values())
    )

    out: List[ReviewRow] = []
    for row in rows:
        request = requests.get(_slug(row.get("review_request")) or "") or {}
        address = _slug(row.get("reviewer")) or ""
        email = emails.get(address) or {}
        reviewed = row.get("reviewed_rev")
        out.append(
            ReviewRow(
                team=_named(teams, request.get("team"), "acronym"),
                kind=_slug(request.get("type")),
                # The address is a poor label but a true one; a reviewer whose
                # person record won't resolve should still appear in the count.
                reviewer=_named(people, email.get("person"), "name")
                or address
                or "(unknown)",
                reviewed_rev=str(reviewed) if str(reviewed or "").isdigit() else None,
                result=_named(results, row.get("result"), "name"),
                state=_slug(row.get("state")) or "unknown",
                when=_date(row.get("completed_on")) or _date(row.get("assigned_on")),
            )
        )
    out.sort(key=lambda row: (row.when or "", row.team or ""))
    return out, fetched, answered


def _positions(doc_name: str) -> Tuple[List[PositionRow], datetime.datetime, bool]:
    """Each balloter's current position — latest event per balloter wins.

    The endpoint returns one row per position *change*, so collapsing is not
    optional: without it a balloter who revised a DISCUSS to No Objection
    appears twice, in both states.
    """
    rows, fetched, answered = _objects(
        f"{_API_BASE}/doc/ballotpositiondocevent/"
        f"?doc__name={quote(doc_name, safe='')}&format=json&limit=100"
    )
    if not rows:
        return [], fetched, answered

    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        balloter = row.get("balloter") or row.get("by") or ""
        when = str(row.get("time") or "")
        if not balloter or not when:
            continue
        held = latest.get(balloter)
        if held is None or when > str(held.get("time") or ""):
            latest[balloter] = row

    people = _fetch_map("person/person", "id", (_slug(uri) for uri in latest))
    out: List[PositionRow] = []
    for balloter, row in latest.items():
        slug = _slug(row.get("pos")) or "norecord"
        # A balloter holding no position has nothing to report; the ballot's
        # shape is the set of positions actually taken.
        if slug in _NO_POSITION:
            continue
        rev = row.get("rev")
        out.append(
            PositionRow(
                balloter=_named(people, balloter, "name") or "(unknown)",
                slug=slug,
                position=_POSITION_LABELS.get(slug, slug.upper()),
                rev=str(rev) if str(rev or "").isdigit() else None,
                when=_date(row.get("time")),
                discuss=bool(str(row.get("discuss") or "").strip()),
            )
        )
    out.sort(key=lambda row: (row.when or "", row.balloter))
    return out, fetched, answered


def _revision_date(doc_name: str, rev: Any) -> Optional[str]:
    """When `rev` of `doc_name` was actually posted, from its
    `newrevisiondocevent`.

    The document's own `time` is its last-modified stamp and advances on any
    later state change, so it dates the revision too late — for
    `draft-ietf-idr-bgp-ls-sr-epe-over-l2bundle` it reads 2026-08-10 for a -09
    posted on 2026-08-05, which renders positions cast on the 6th as though
    they predated the text they were cast against. In a tool whose entire
    payload is revision attribution, a header that contradicts its own table
    is worse than one extra request.
    """
    if not isinstance(rev, str) or not rev:
        return None
    rows, _, _ = _objects(
        f"{_API_BASE}/doc/newrevisiondocevent/"
        f"?doc__name={quote(doc_name, safe='')}&rev={quote(rev, safe='')}"
        "&format=json&limit=1"
    )
    return _date(rows[0].get("time")) if rows else None


def fetch_review_record(
    name: str,
) -> Tuple[Optional[ReviewRecord], datetime.datetime]:
    """The joined review and ballot record for one draft.

    `(None, fetched_at)` when Datatracker has no such document — the same
    contract as `fetch_draft_status`, so a mistyped name is reported rather
    than rendered as a document nobody has reviewed.
    """
    canonical = normalize_draft_name(name)
    doc, fetched = _cached_json(f"{_API_BASE}/doc/document/{canonical}/?format=json")
    if doc is None or not doc.get("name"):
        return None, fetched

    reviews, reviews_fetched, reviews_ok = _reviews(canonical)
    positions, positions_fetched, positions_ok = _positions(canonical)
    rev = doc.get("rev")
    record = ReviewRecord(
        name=str(doc.get("name")),
        rev=str(rev) if isinstance(rev, str) and rev else None,
        rev_date=_revision_date(canonical, rev) or _date(doc.get("time")),
        reviews=reviews,
        positions=positions,
        unavailable=[
            half
            for half, ok in (("reviews", reviews_ok), ("ballot", positions_ok))
            if not ok
        ],
    )
    return record, min(fetched, reviews_fetched, positions_fetched)
