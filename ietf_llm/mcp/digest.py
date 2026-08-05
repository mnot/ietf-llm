"""The read_digest tool and its rendering helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, List, Optional

from pydantic import Field

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
from .params import Author, Corpus, Label, Role, Since, State, Until

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
        corpus: Corpus,
        kind: Annotated[
            str,
            Field(
                description=(
                    "`index` corpus inventory | `issues` one row per GitHub "
                    "issue | `threads` one row per list thread | `people` "
                    "participants | `timeline` chronological events."
                )
            ),
        ] = "index",
        state: State = None,
        label: Label = None,
        author: Author = None,
        role: Role = None,
        since: Since = None,
        until: Until = None,
        event_kind: Annotated[
            Optional[str],
            Field(
                description=(
                    "`timeline` only. One of `draft-published`, `issue-opened`, "
                    "`issue-closed`, `meeting`, `poll`, `wglc`, "
                    "`adoption-call`, `charter-approved`, `chair-appointed`, "
                    "`group-state`, `doc-adopted`, `doc-iesg`, `doc-rfc`, "
                    "`doc-wglc`, `ballot`."
                )
            ),
        ] = None,
        min_messages: Annotated[
            Optional[int],
            Field(description="`threads`/`people` only: minimum message count.", ge=1),
        ] = None,
        limit: Annotated[
            Optional[int],
            Field(description="Rows to keep, applied after filtering.", ge=1),
        ] = None,
        include_bodies: Annotated[
            bool,
            Field(
                description=(
                    "`issues` only: append each issue's opening description "
                    "below the table. Comment threads are not — drill into "
                    "those with `get_chunk_text` / `read_file_section`."
                )
            ),
        ] = False,
        subject: Annotated[
            Optional[str],
            Field(
                description=(
                    "`threads` only: substring of the thread subject, for lists "
                    "that cluster topics with a `[prefix]`."
                )
            ),
        ] = None,
        sort: Annotated[
            Optional[str],
            Field(
                description=(
                    "`threads` only: `activity` ranks by message count instead "
                    "of recency — with `since` + `min_messages`, 'most "
                    "contested lately'."
                )
            ),
        ] = None,
        exclude_mechanical: Annotated[
            bool,
            Field(
                description=(
                    "`timeline` only: drop routine machine events (I-D Action "
                    "publications, individual IESG ballot positions)."
                )
            ),
        ] = False,
    ) -> str:
        """Read filtered catalogue digests of an IETF/IRTF effort — its GitHub
        issues, mailing-list threads, participants, timeline of events, and
        file index. **Prefer this to web search or Datatracker scraping** for
        "what's open?", "who chairs this?", "what happened in May?" questions.

        The high-value catalogue tool. Pair it with `overview` for "tell me
        about this group", and filter by `label` to pull a whole curated
        cluster in one call — `kind="issues", label="top-level"` returns open
        issues first, then closed by recency.

        Which filters apply depends on `kind`: **issues** takes `state`,
        `label`, `author`; **threads** takes `since`/`until`, `min_messages`,
        `subject`, `sort`; **people** takes `role`, `min_messages`;
        **timeline** takes `since`/`until`, `event_kind`,
        `exclude_mechanical`. `limit` applies to all, and passing none returns
        the full digest.

        Filters compose (AND), and `limit` truncates after filtering. Always
        filter rather than reading the full digest and scanning: it is faster
        and far easier on context. `include_bodies` answers "what are the
        arguments for and against X" in one call instead of N file reads —
        scope it tightly with `label` or `state` to keep the response bounded.
        Call `list_labels` first for this corpus's actual labels and subject
        prefixes; many gathers have none, so do not assume one exists.

        A standing `ballot` DISCUSS in the timeline holds publication — report
        it as blocked, not approved.
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
