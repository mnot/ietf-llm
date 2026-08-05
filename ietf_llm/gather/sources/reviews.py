"""Directorate / Last Call reviews written *by* a person.

A follow-an-author corpus (`--author`) tracks the drafts someone wrote.
Their **reviews** are the other half of the record, and the more useful
half for anyone asking "what does this person look for in a document":
a review is that question already answered, in their own words, against
a document they did not write.

Datatracker models these as review *assignments*. Each completed
assignment names the reviewing team (httpdir, secdir, genart, …), the
review type (`early` / `lc` / `telechat`), the document and revision
reviewed, and a result slug (`ready-issues`, `not-ready`, …). The review
text itself is a Document of type `review`, whose only public rendering
is the datatracker doc page — there is no `.txt` mirror, and the
`mailarch_url` field is null in practice — so the body is scraped from
that page's `<pre class="pasted">` block.

Scoping. Reviews are additive and immutable once completed: a review
from 2015 is as much a part of how this person reviews as one from last
month, and unlike ballots there is no "current" version to supersede an
old one. So there is no `--months` window here — we take them all. The
volume is bounded by how much reviewing the person has actually done
(tens, not thousands), and each body is fetched once and then rides the
HTTP cache's ETag revalidation on re-gather.

Storage. One markdown file per completed review at
`reviews/<review-doc-name>.md` — the datatracker document name, which
already encodes draft, revision, team, type, reviewer, and date, and so
sorts and dedupes for free. Assignments that were never completed (no
response, rejected, still open) have no text and are skipped; the count
is logged so a caller can tell "no reviews" from "no completed reviews".
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from ...atomicio import write_if_changed
from ...log import LogLevel, Verbosity, log
from ...net.transport import fetch_resource
from ...paths import review_path, reviews_dir
from .datatracker import _get_json  # pylint: disable=protected-access

_API_BASE = "https://datatracker.ietf.org/api/v1"
_DOC_URL = "https://datatracker.ietf.org/doc/"

#: Datatracker result slug → the label the review page shows. Falls back
#: to the raw slug, so a new result name renders readably without a code
#: change (same convention as `ballots._POSITION_LABELS`).
_RESULT_LABELS = {
    "serious-issues": "Serious Issues",
    "issues": "Has Issues",
    "nits": "Has Nits",
    "not-ready": "Not Ready",
    "right-track": "On the Right Track",
    "almost-ready": "Almost Ready",
    "ready-issues": "Ready with Issues",
    "ready-nits": "Ready with Nits",
    "ready": "Ready",
    "clarification-needed": "Clarification Needed",
}

#: Review type slug → label.
_TYPE_LABELS = {
    "early": "Early",
    "lc": "IETF Last Call",
    "telechat": "Telechat",
}

_SLUG_FROM_URL = re.compile(r"/([^/]+)/?$")
_PRE_BLOCK = re.compile(r'<pre class="pasted">(.*?)</pre>', re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_WS_RUN = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    """Whitespace-insensitive form, for comparing two renderings of the
    same string (the API's request comment vs. the page's block, which
    may have been re-wrapped)."""
    return _WS_RUN.sub(" ", text).strip()


@dataclass
class Review:
    """One completed review, ready to render."""

    doc_name: str  # review-ietf-ppm-dap-09-httpdir-early-nottingham-2023-12-29
    reviewer: str  # canonical name of the person who wrote it
    reviewed_doc: str  # draft-ietf-ppm-dap
    reviewed_rev: str  # "09", or "" when the assignment didn't record one
    team: str  # httpdir, secdir, genart, … ("" if unresolved)
    review_type: str  # early / lc / telechat slug
    result: str  # result slug ("" when the assignment recorded none)
    assigned_on: str  # ISO 8601, as Datatracker returns it
    completed_on: str
    request_comment: str  # why the review was requested; often empty
    text: str = ""  # the review body, filled by `fetch_review_bodies`

    @property
    def url(self) -> str:
        return f"{_DOC_URL}{self.doc_name}/"

    @property
    def reviewed_label(self) -> str:
        """`draft-foo-09` when a revision was recorded, else `draft-foo`."""
        if self.reviewed_rev:
            return f"{self.reviewed_doc}-{self.reviewed_rev}"
        return self.reviewed_doc


def _slug(uri: Any) -> str:
    """Trailing path segment of a Datatracker resource URI, or ""."""
    match = _SLUG_FROM_URL.search(str(uri or "").rstrip("/"))
    return match.group(1) if match else ""


def fetch_person_emails(
    person_id: int, verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Every email address Datatracker knows for `person_id`.

    The review API filters by *reviewer email*, not person, and a
    long-serving participant has several addresses on file (employer
    addresses from past jobs, a personal one). Querying only the address
    the corpus was seeded with would silently drop every review filed
    under an older affiliation, so we expand to the full set first.
    """
    body = _get_json(f"{_API_BASE}/person/email/?person={person_id}&limit=100")
    addresses = [
        str(obj["address"])
        for obj in (body or {}).get("objects") or []
        if obj.get("address")
    ]
    log(
        f"  {len(addresses)} known email address(es) for person {person_id}.",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return addresses


def _iter_assignments(email: str) -> Iterable[Dict[str, Any]]:
    """Page through every review assignment for one reviewer address."""
    path: Optional[str] = (
        f"{_API_BASE}/review/reviewassignment/?reviewer={email}&limit=100"
    )
    while path:
        body = _get_json(path)
        if not body:
            return
        yield from body.get("objects") or []
        path = (body.get("meta") or {}).get("next") or None


def _fetch_requests(request_ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """Batch-dereference review requests by id.

    Tastypie honours `id__in`, so N assignments cost ceil(N/100) list
    queries rather than N GETs. The request carries the reviewed
    document, the team, and the review type — none of which are on the
    assignment itself.
    """
    out: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(request_ids)
    for start in range(0, len(ordered), 100):
        batch = ",".join(ordered[start : start + 100])
        body = _get_json(f"{_API_BASE}/review/reviewrequest/?id__in={batch}&limit=100")
        for obj in (body or {}).get("objects") or []:
            if obj.get("id") is not None:
                out[str(obj["id"])] = obj
    return out


def _fetch_team_acronyms(team_ids: Set[str]) -> Dict[str, str]:
    """Batch-resolve review-team group ids to acronyms (`httpdir`, …)."""
    out: Dict[str, str] = {}
    ordered = sorted(team_ids)
    for start in range(0, len(ordered), 100):
        batch = ",".join(ordered[start : start + 100])
        body = _get_json(f"{_API_BASE}/group/group/?id__in={batch}&limit=100")
        for obj in (body or {}).get("objects") or []:
            if obj.get("id") is not None and obj.get("acronym"):
                out[str(obj["id"])] = str(obj["acronym"])
    return out


def fetch_reviews(
    person_id: int,
    reviewer_name: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[Review]:
    """Every completed review written by `person_id`, without bodies.

    Bodies are a separate (and much more expensive) pass — see
    `fetch_review_bodies` — so a caller can report the catalogue before
    committing to the page fetches.
    """
    assignments: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    for email in fetch_person_emails(person_id, verbose):
        for obj in _iter_assignments(email):
            # A person's addresses are distinct, so the same assignment
            # should not appear twice — but dedupe anyway rather than
            # trust that, since a duplicate would write the same file
            # twice and inflate the counts we log.
            ident = obj.get("id")
            if ident is not None and ident in seen_ids:
                continue
            if ident is not None:
                seen_ids.add(int(ident))
            assignments.append(obj)

    completed = [
        obj
        for obj in assignments
        if _slug(obj.get("state")) == "completed" and obj.get("review")
    ]
    skipped = len(assignments) - len(completed)
    if skipped:
        log(
            f"  {skipped} review assignment(s) with no completed review "
            "(declined, no response, or still open) — skipped.",
            verbose,
            level=LogLevel.PROGRESS,
        )

    requests = _fetch_requests(
        {
            _slug(obj.get("review_request"))
            for obj in completed
            if obj.get("review_request")
        }
    )
    teams = _fetch_team_acronyms(
        {_slug(req.get("team")) for req in requests.values() if req.get("team")}
    )

    reviews: List[Review] = []
    for obj in completed:
        request = requests.get(_slug(obj.get("review_request")), {})
        reviews.append(
            Review(
                doc_name=_slug(obj.get("review")),
                reviewer=reviewer_name,
                reviewed_doc=_slug(request.get("doc")),
                reviewed_rev=str(obj.get("reviewed_rev") or ""),
                team=teams.get(_slug(request.get("team")), ""),
                review_type=_slug(request.get("type")),
                result=_slug(obj.get("result")),
                assigned_on=str(obj.get("assigned_on") or ""),
                completed_on=str(obj.get("completed_on") or ""),
                request_comment=str(request.get("comment") or ""),
            )
        )
    reviews.sort(key=lambda r: (r.completed_on, r.doc_name))
    log(
        f"  {len(reviews)} completed review(s).",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return reviews


def fetch_review_text(review: Review) -> Optional[str]:
    """Scrape one review's body from its datatracker document page.

    Review documents have no plain-text mirror (`www.ietf.org/archive/id/
    <name>.txt` 404s) and no text field on the API, so the doc page is
    the only public source. The body sits in a `<pre class="pasted">`
    block — but so does the *request* comment when the requester left
    one, and it renders first. We drop any block matching the known
    request comment and take the last of what remains, so a page with
    both blocks yields the review rather than the request.

    Datatracker linkifies document references *inside* the block
    (`<a href="/doc/rfc6690/">RFC6690</a>`), so tags are stripped —
    before unescaping, not after: the review's own literal angle
    brackets arrive escaped (`&lt;pre&gt;`), and unescaping first would
    turn them into markup the strip then eats.

    Returns None when the page is unreachable or has no usable block;
    the caller skips that review rather than writing an empty file.
    """
    response = fetch_resource(review.url)
    if response is None:
        return None
    blocks = [
        html.unescape(_HTML_TAG.sub("", match.group(1))).strip()
        for match in _PRE_BLOCK.finditer(response.text)
    ]
    comment = _collapse_ws(review.request_comment)
    body = [block for block in blocks if block and _collapse_ws(block) != comment]
    return body[-1] if body else None


def fetch_review_bodies(
    reviews: List[Review], verbose: Verbosity = Verbosity.STATUS
) -> List[Review]:
    """Fill in `text` for each review; drop the ones we couldn't fetch."""
    filled: List[Review] = []
    for review in reviews:
        text = fetch_review_text(review)
        if text is None:
            log(
                f"  No review text found at {review.url} — skipped.",
                verbose,
                level=LogLevel.WARN,
            )
            continue
        review.text = text
        filled.append(review)
    return filled


def render_review(review: Review) -> str:
    """Markdown for one review file."""
    result = _RESULT_LABELS.get(review.result, review.result)
    rtype = _TYPE_LABELS.get(review.review_type, review.review_type)
    title = f"{rtype} review of {review.reviewed_label}".strip()

    lines = [f"# {title}", ""]
    lines.append(f"**Reviewer:** {review.reviewer}  ")
    if review.team:
        lines.append(f"**Team:** {review.team}  ")
    lines.append(f"**Document:** {review.reviewed_label}  ")
    if result:
        lines.append(f"**Result:** {result}  ")
    if review.completed_on:
        # Date only — the time of day is noise, and the chunker's date
        # re-parse wants a plain ISO date.
        lines.append(f"**Completed:** {review.completed_on[:10]}  ")
    lines.append(f"**URL:** {review.url}")
    lines.append("")
    if review.request_comment:
        lines.append(f"_Requested with the note: {review.request_comment}_")
        lines.append("")
    lines.append("## Review")
    lines.append("")
    lines.append(review.text.strip())
    lines.append("")
    return "\n".join(lines)


def write_review_files(
    cache_dir: str, reviews: List[Review], verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Write `reviews/<doc-name>.md` for each review. Returns the paths."""
    if not reviews:
        return []
    directory = reviews_dir(cache_dir)
    os.makedirs(directory, exist_ok=True)
    written: List[str] = []
    changed = 0
    for review in reviews:
        if not review.doc_name:
            continue
        path = review_path(cache_dir, review.doc_name)
        if write_if_changed(path, render_review(review)):
            changed += 1
        written.append(path)
    log(
        f"Reviews: {len(written)} file(s) ({changed} written).",
        verbose,
        level=LogLevel.STATUS,
    )
    return written


def gather_reviews(
    cache_dir: str,
    person_id: int,
    reviewer_name: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Fetch and write every completed review by `person_id`.

    The whole author-side review pass, in one call for the sequencer.
    """
    log(
        f"Gathering reviews written by {reviewer_name}...",
        verbose,
        level=LogLevel.STATUS,
    )
    reviews = fetch_reviews(person_id, reviewer_name, verbose)
    if not reviews:
        return []
    return write_review_files(cache_dir, fetch_review_bodies(reviews, verbose), verbose)
