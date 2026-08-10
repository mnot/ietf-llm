"""Per-PR Markdown files for GitHub pull requests — symmetric with the
per-issue files written by `issue_files`.

The i-d-template `archive.json` we already download for issues carries a
`pulls` array alongside `issues`; until now we dropped it on the floor.
That was a real loss for anyone reading the record: the *reasoning* behind
a change usually lives in the PR, not the issue. "Remove some extraneous
2119 terms" is a PR title; the issue that later asks why the keywords went
away has no way to reach it. And with `mergeCommit` recorded here, a reader
can walk changed text → commit → PR → the issue it closes entirely offline.

For each PR we emit:

  <cache>/files/pulls/<repo-slug>/<N>.md

GitHub numbers issues and PRs in one sequence, so `pulls/<repo>/34.md` and
`issues/<repo>/34.md` never both exist — but they get separate trees anyway,
because a PR is a different kind of record: it has a merge disposition, a
head/base branch, and reviews, and none of that belongs in the issue schema.

The file format deliberately matches `issue_files._render_issue` — metadata
header, outline, `### [N] DATE — Author` sections — because that shape is
what the chunker (`embeddings.chunking._chunk_thread_file`) cuts on, and
what `get_chunk_text` / `find_replies` speak.

Two places where the PR shape forces a different decision from issues:

- **State vs disposition.** `**State:**` is normalised to OPEN / CLOSED so
  the shared search facet (`state="closed"`) keeps meaning the same thing
  across both trees; a merged PR is a closed one. The precise outcome —
  merged vs closed-unmerged, by whom, into which commit — goes on a separate
  `**Disposition:**` line.
- **Reviews.** Most reviews are bodiless approvals (on httpwg/http-extensions,
  3673 reviews carry only 701 bodies), so rendering one section each would
  bury the substance in ceremony. The verdict tally goes in the header and
  only reviews that actually say something get a section. Inline review
  comments carry no `author` of their own in the archive — just
  `originalPosition`, `body` and timestamps — so they render as a nested
  list under their parent review, attributed to the reviewer.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ...log import Verbosity
from ...paths import (
    github_dir,
    pull_path,
    pull_repo_dir,
    pulls_dir,
)
from ...people import Registry
from .github_records import last_comment_quote, write_record_files
from .issue_files import (
    _canon_github,
    _canon_with_role,
    _format_iso_to_minute,
    _normalise_html,
)

# GitHub's closing keywords, as accepted in a PR title or body. Anchored to
# `#N` so a bare "fixes the parser" doesn't match. GitHub itself accepts
# these in any case and with an optional colon after the keyword.
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b",
    re.IGNORECASE,
)

#: Review verdicts, in the order we summarise them — most consequential
#: first, so a reader scanning the header sees objections before approvals.
_REVIEW_VERDICTS = ("CHANGES_REQUESTED", "APPROVED", "DISMISSED", "COMMENTED")

#: How each verdict reads in the header tally.
_VERDICT_LABEL = {
    "CHANGES_REQUESTED": "changes requested by",
    "APPROVED": "approved by",
    "DISMISSED": "dismissed review from",
    "COMMENTED": "commented on by",
}


def _closes(pull: Dict[str, Any]) -> List[int]:
    """Issue numbers this PR declares it closes, from title + body.

    Deduped, in first-seen order. Self-references are dropped: a PR
    claiming to close its own number is a typo, not a link.
    """
    own = pull.get("number")
    found: List[int] = []
    for text in (pull.get("title") or "", pull.get("body") or ""):
        for match in _CLOSES_RE.finditer(str(text)):
            try:
                number = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if number == own or number in found:
                continue
            found.append(number)
    return found


def _normalised_state(pull: Dict[str, Any]) -> str:
    """`OPEN` or `CLOSED` — the shared facet the chunker and search read.

    MERGED collapses into CLOSED on purpose: a merged PR is resolved, and
    `search_corpus(state="closed")` should find it alongside the closed
    issues it settled. The distinction survives on the Disposition line.
    """
    state = (pull.get("state") or "").upper()
    return "OPEN" if state == "OPEN" else "CLOSED"


def _disposition(pull: Dict[str, Any], registry: Optional[Registry]) -> Optional[str]:
    """One line saying how the PR ended: merged (by whom, into which
    commit) or closed without merging.

    This replaces the issue files' "Closing rationale" heuristic, which
    does not transfer: a merged PR's last comment is usually "thanks" or
    absent entirely, while the merge itself is the resolution. The commit
    oid is the hinge of the offline blame → commit → PR → issue walk, so
    it is emitted in full even though we show an abbreviated form inline.
    """
    state = (pull.get("state") or "").upper()
    if state == "OPEN":
        return None
    if state == "MERGED":
        when = _format_iso_to_minute(pull.get("mergedAt") or pull.get("closedAt"))
        who = pull.get("mergedBy") or ""
        parts = ["merged"]
        if who:
            parts.append(f"by {_canon_with_role(registry, who)}")
        if when:
            parts.append(f"on {when}")
        commit = pull.get("mergeCommit") or {}
        oid = commit.get("oid") if isinstance(commit, dict) else None
        if isinstance(oid, str) and oid:
            parts.append(f"(commit `{oid}`)")
        return " ".join(parts)
    when = _format_iso_to_minute(pull.get("closedAt"))
    return f"closed without merging on {when}" if when else "closed without merging"


def _closing_note(pull: Dict[str, Any], registry: Optional[Registry]) -> Optional[str]:
    """For a PR closed *without* merging, the last comment as a blockquote.

    Only for the unmerged case, where a human explanation ("superseded by
    #57", "not going this way") is both likely and load-bearing. A merged
    PR needs no such note — the Disposition line already says what happened.
    """
    if (pull.get("state") or "").upper() != "CLOSED":
        return None
    return last_comment_quote(
        pull.get("comments") or [],
        _format_iso_to_minute,
        lambda login: _canon_with_role(registry, login),
    )


def _substantive_reviews(pull: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reviews worth their own section: those with a body or inline
    comments. A bare APPROVED is counted in the header tally instead."""
    out: List[Dict[str, Any]] = []
    for review in pull.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        if (review.get("body") or "").strip() or review.get("comments"):
            out.append(review)
    return out


def _review_summary(pull: Dict[str, Any], registry: Optional[Registry]) -> str:
    """`changes requested by A · approved by B, C` — the verdict tally.

    One entry per reviewer per verdict, deduped: a reviewer who approves
    twice is named once. Reviewers who only ever left a COMMENTED review
    are folded in last, since "commented" is the default GitHub gives a
    review with no explicit verdict.
    """
    by_verdict: Dict[str, List[str]] = {}
    for review in pull.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        verdict = (review.get("state") or "COMMENTED").upper()
        if verdict not in _VERDICT_LABEL:
            continue
        name = _canon_github(registry, review.get("author") or "")
        names = by_verdict.setdefault(verdict, [])
        if name not in names:
            names.append(name)
    parts: List[str] = []
    for verdict in _REVIEW_VERDICTS:
        verdict_names = by_verdict.get(verdict)
        if verdict_names:
            parts.append(f"{_VERDICT_LABEL[verdict]} {', '.join(verdict_names)}")
    return "  ·  ".join(parts)


def _participants(pull: Dict[str, Any], registry: Optional[Registry]) -> List[str]:
    """Canonical names of everyone who touched the PR: author, commenters,
    reviewers, and whoever merged it."""
    logins: List[str] = [str(pull.get("author") or "")]
    logins += [str(c.get("author") or "") for c in (pull.get("comments") or [])]
    logins += [
        str(r.get("author") or "")
        for r in (pull.get("reviews") or [])
        if isinstance(r, dict)
    ]
    logins.append(str(pull.get("mergedBy") or ""))
    seen: List[str] = []
    seen_set: set[str] = set()
    for login in logins:
        if not login:
            continue
        name = _canon_github(registry, login)
        if name in seen_set:
            continue
        seen_set.add(name)
        seen.append(name)
    return seen


def _timeline(pull: Dict[str, Any]) -> List[Tuple[str, str, Dict[str, Any]]]:
    """PR comments and substantive reviews interleaved by timestamp.

    Returns `(createdAt, kind, record)` where kind is `comment` or
    `review`. One merged sequence so the `### [N]` numbering the chunker
    keys on stays a single ordered run, exactly as in a thread or issue
    file.
    """
    events: List[Tuple[str, str, Dict[str, Any]]] = []
    for comment in pull.get("comments") or []:
        if isinstance(comment, dict):
            events.append((str(comment.get("createdAt") or ""), "comment", comment))
    for review in _substantive_reviews(pull):
        events.append((str(review.get("createdAt") or ""), "review", review))
    events.sort(key=lambda event: event[0])
    return events


def _render_inline_comments(review: Dict[str, Any]) -> List[str]:
    """Inline (diff-anchored) review comments as a nested bullet list.

    The archive records only `originalPosition`, `body` and timestamps for
    these — no author (they inherit the review's) and no file path or diff
    hunk, so there is nothing to anchor them to beyond their order. We
    render them as a list under the review rather than inventing a
    precision the data doesn't have.
    """
    out: List[str] = []
    inline = [c for c in (review.get("comments") or []) if isinstance(c, dict)]
    bodies = [(_normalise_html((c.get("body") or "").strip())) for c in inline]
    bodies = [b for b in bodies if b]
    if not bodies:
        return out
    out.append(f"_{len(bodies)} inline comment(s) on the diff:_\n")
    for body in bodies:
        lines = body.splitlines() or [""]
        out.append(f"- {lines[0]}")
        out.extend(f"  {line}" for line in lines[1:])
    out.append("")
    return out


def _render_pull(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    repo: str, pull: Dict[str, Any], registry: Optional[Registry]
) -> str:
    number = pull.get("number", "?")
    title = pull.get("title") or "(no title)"
    author_name = _canon_with_role(registry, pull.get("author") or "")
    opened = _format_iso_to_minute(pull.get("createdAt"))
    updated = _format_iso_to_minute(pull.get("updatedAt"))
    labels = ", ".join(
        lbl for lbl in (pull.get("labels") or []) if isinstance(lbl, str)
    )
    comments = pull.get("comments") or []
    reviews = pull.get("reviews") or []
    participants = _participants(pull, registry)

    out: List[str] = []
    out.append(f"# Pull request #{number}: {title}\n")
    out.append(f"**Repository:** {repo}  ")
    if "/" in repo and number != "?":
        out.append(f"**URL:** https://github.com/{repo}/pull/{number}  ")
    # Normalised, not verbatim — see _normalised_state. The verbatim
    # outcome is on the Disposition line below.
    out.append(f"**State:** {_normalised_state(pull)}  ")
    out.append(f"**Opened by:** {author_name} on {opened}  ")
    if updated and updated != opened:
        out.append(f"**Last updated:** {updated}  ")
    base = pull.get("baseRefName") or ""
    head = pull.get("headRefName") or ""
    if base and head:
        out.append(f"**Branch:** `{head}` → `{base}`  ")
    if labels:
        out.append(f"**Labels:** {labels}  ")
    out.append(
        f"**Comments:** {len(comments)}  ·  **Reviews:** {len(reviews)}  ·  "
        f"**Participants ({len(participants)}):** " + ", ".join(participants)
    )
    closes = _closes(pull)
    if closes:
        out.append("**Closes:** " + ", ".join(f"#{n}" for n in closes))
    disposition = _disposition(pull, registry)
    if disposition:
        out.append(f"**Disposition:** {disposition}")
    review_summary = _review_summary(pull, registry)
    if review_summary:
        out.append(f"**Review verdicts:** {review_summary}")
    closing_note = _closing_note(pull, registry)
    if closing_note:
        out.append("")
        out.append("**Closing note:**\n")
        out.append(closing_note)
    out.append("")

    events = _timeline(pull)
    if events:
        out.append("## Outline\n")
        out.append(f"- **[1]** {opened} — {author_name} _(opened pull request)_")
        for idx, (when, kind, record) in enumerate(events, 2):
            e_when = _format_iso_to_minute(when)
            e_author = _canon_with_role(registry, record.get("author") or "")
            tag = (
                f" _(review: {(record.get('state') or 'COMMENTED').upper()})_"
                if kind == "review"
                else ""
            )
            out.append(f"- **[{idx}]** {e_when} — {e_author}{tag}")
        out.append("")

    out.append("## Description\n")
    out.append(f"### [1] {opened} — {author_name} _(opened pull request)_\n")
    body = _normalise_html((pull.get("body") or "").strip())
    out.append(body or "_(no description provided)_")
    out.append("")

    if events:
        out.append("## Discussion\n")
        for idx, (when, kind, record) in enumerate(events, 2):
            e_when = _format_iso_to_minute(when)
            e_author = _canon_with_role(registry, record.get("author") or "")
            if kind == "review":
                verdict = (record.get("state") or "COMMENTED").upper()
                out.append(f"### [{idx}] {e_when} — {e_author} _(review: {verdict})_\n")
                review_body = _normalise_html((record.get("body") or "").strip())
                if review_body:
                    out.append(review_body)
                    out.append("")
                out.extend(_render_inline_comments(record))
            else:
                out.append(f"### [{idx}] {e_when} — {e_author}\n")
                c_body = _normalise_html((record.get("body") or "").strip())
                out.append(c_body or "_(empty comment)_")
                out.append("")
    return "\n".join(out) + "\n"


def write_pull_files(
    cache_dir: str,
    registry: Optional[Registry] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """For each cached `github/<repo-slug>.json`, write per-PR .md files
    under `pulls/<repo-slug>/<N>.md`.

    Archives with no `pulls` key write nothing: the REST fallback in
    `github.download_github_issues` builds `{repo, timestamp, issues}`
    only (and skips PRs outright), so a repo without a gh-pages
    `archive.json` simply has no PR record here. That is deliberate —
    fetching PRs over the API would cost the calls and credentials the
    archive lets us avoid.

    Write-if-changed, with the same orphan sweep as `write_issue_files`:
    a PR that leaves the archive loses its file on the next gather.
    """
    if not os.path.isdir(cache_dir):
        return []

    archives_dir = github_dir(cache_dir)
    if not os.path.isdir(archives_dir):
        return []

    return write_record_files(
        archives_dir,
        pulls_dir(cache_dir),
        "pulls",
        lambda repo: pull_repo_dir(cache_dir, repo),
        lambda repo, number: pull_path(cache_dir, repo, number),
        lambda repo, pull: _render_pull(repo, pull, registry),
        "PR",
        verbose,
    )
