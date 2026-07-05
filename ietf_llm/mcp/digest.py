"""The read_digest tool and its rendering helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional

from ..digest.query import query_digest
from .common import (
    _digest_path,
    _invalid_date_message,
    _missing_digest_message,
    _offload,
    _requires_corpus,
    _safe_path,
    _with_freshness,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


@_requires_corpus
def tool_read_digest(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    wg: str,
    kind: str = "index",
    state: Optional[str] = None,
    label: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    event_kind: Optional[str] = None,
    min_messages: Optional[int] = None,
    limit: Optional[int] = None,
    include_bodies: bool = False,
    subject: Optional[str] = None,
    sort: Optional[str] = None,
    exclude_mechanical: bool = False,
) -> str:
    path = _digest_path(wg, kind)
    if not path:
        return _missing_digest_message(wg, kind)
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    filtered = query_digest(
        path,
        kind,
        state=state,
        label=label,
        author=author,
        role=role,
        since=since,
        until=until,
        event_kind=event_kind,
        min_messages=min_messages,
        limit=limit,
        subject=subject,
        sort=sort or None,
        exclude_mechanical=exclude_mechanical or None,
    )
    if include_bodies and kind == "issues":
        filtered = filtered + _append_issue_bodies(wg, filtered)
    return _with_freshness(wg, filtered)


# Regex tuned to the issues-digest schema: the File column carries a
# backtick-wrapped relative path under `issues/<repo>/<N>.md`. Picking
# it up from the rendered markdown is more robust than re-parsing the
# table — this works whether or not `summarize` is active (which would
# shift column positions).
_ISSUE_FILE_CELL_RE = re.compile(r"`(issues/\S+\.md)`")


def _append_issue_bodies(wg: str, filtered_markdown: str) -> str:
    """Append the description body (and frontmatter) of each filtered
    issue to a read_digest('issues') response.

    The collected bodies come straight from the per-issue files — which
    already carry state, labels, participants, duplicate-of, closing
    rationale, and the issue's opening description. We slice through
    the start of `## Comments` so we don't pull the full comment
    history (that's what `get_chunk_text(end_chunk_idx=...)` is for).
    A consuming LLM asking "what are the for/against arguments on
    label=top-level" gets everything they need in one round-trip.
    """
    filenames: List[str] = []
    seen: set[str] = set()
    for match in _ISSUE_FILE_CELL_RE.finditer(filtered_markdown):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        filenames.append(name)
    if not filenames:
        return ""
    chunks: List[str] = ["\n\n## Issue bodies\n"]
    chunks.append(
        f"_{len(filenames)} issue(s) below — frontmatter + opening "
        "description per issue. Use `get_chunk_text` or `read_file_section` "
        "to read full comment threads._\n"
    )
    for name in filenames:
        path = _safe_path(wg, name)
        if path is None:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        # Cut at "## Comments" — the comment history is the bulky part
        # and the consumer can drill into it on demand.
        cutoff = text.find("\n## Comments")
        if cutoff != -1:
            text = text[:cutoff].rstrip() + "\n"
        chunks.append("\n---\n")
        chunks.append(text)
    return "".join(chunks)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def read_digest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        kind: str = "index",
        state: Optional[str] = None,
        label: Optional[str] = None,
        author: Optional[str] = None,
        role: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        event_kind: Optional[str] = None,
        min_messages: Optional[int] = None,
        limit: Optional[int] = None,
        include_bodies: bool = False,
        subject: Optional[str] = None,
        sort: Optional[str] = None,
        exclude_mechanical: bool = False,
    ) -> str:
        """Read filtered catalogue digests of an IETF/IRTF effort — its
        GitHub issues, mailing-list threads, participants (people),
        timeline of events, and file index. **Prefer this to web
        search or Datatracker scraping** for "what's open?", "who chairs
        this?", "what happened in May?"-shaped questions about a working
        group, research group, or mailing list. The high-value catalogue
        tool — pair with `overview` for "tell me about this group"-shaped
        questions, and use `label=` here to get
        every issue tagged with a topic in one call (e.g. `kind="issues",
        label="top-level"` returns the whole curated cluster, open
        issues first then closed-by-recency).

        `include_bodies=True` (issues only) appends each filtered
        issue's frontmatter + opening description below the catalogue
        table, so "what are the arguments for/against X" questions can
        be answered in ONE call instead of N follow-up file reads.
        Comment threads are NOT included — use `get_chunk_text` or
        `read_file_section` to drill into them on demand. Scope tightly
        with `label=` or `state=` to keep the response bounded.

        kind = "index"    — corpus inventory + how-to-use pointer
             | "issues"   — one row per GitHub issue. Filters: state
                            ("open"/"closed"), label (substring),
                            author (substring), limit (int).
             | "threads"  — one row per mailing list thread. Filters:
                            since/until ("YYYY-MM-DD"), min_messages,
                            limit, subject (substring on the thread
                            subject — high-value for WGs that cluster
                            topics on the list with `[xxx]` prefixes).
                            Call `list_labels` first for THIS corpus's
                            actual prefixes; many gathers have none, so
                            do not assume a specific one (e.g. `[mlkem]`)
                            exists.
                            `sort="activity"` ranks by message count
                            (where the back-and-forth is) instead of
                            recency — pair with `since=` + `min_messages=`
                            for "most contested lately".
             | "people"   — participants. Filters: role (substring,
                            e.g. "Chair"), min_messages, limit.
             | "timeline" — chronological events. Filters: since/until,
                            event_kind (drafts: "draft-published";
                            issues: "issue-opened" / "issue-closed";
                            meetings: "meeting"; session polls:
                            "poll"; procedural: "wglc" / "adoption-call";
                            Datatracker governance: "charter-approved" /
                            "chair-appointed" / "group-state" /
                            "doc-adopted" / "doc-iesg" / "doc-rfc" /
                            "doc-wglc" / "ballot"), limit. A standing
                            "ballot" DISCUSS holds publication — report
                            it as blocked, not approved.
                            `exclude_mechanical=True` drops the routine
                            machine events (I-D Action publications and
                            individual IESG ballot positions) so the human
                            discussion / decision events stand out.

        Pass no filters to get the full digest (same bytes as before).
        Filters compose (AND); `limit` truncates after filtering.
        For catalogue-style queries (e.g. "open issues with label X"),
        always use filters rather than reading the full digest and
        scanning — both faster and easier on context.
        """
        return await _offload(
            tool_read_digest,
            corpus,
            kind,
            state=state,
            label=label,
            author=author,
            role=role,
            since=since,
            until=until,
            event_kind=event_kind,
            min_messages=min_messages,
            limit=limit,
            include_bodies=include_bodies,
            subject=subject,
            sort=sort,
            exclude_mechanical=exclude_mechanical,
        )
