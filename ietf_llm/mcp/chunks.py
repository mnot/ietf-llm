"""Chunk + raw-file readers: get_chunk_text, get_chunks_batch,
get_by_url, read_file_section."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional

from pydantic import Field

from ..embeddings import chunk_counts, find_chunks_by_url, get_chunk
from ..freshness import gather_enabled
from ..paths import digest_kind_from_relpath
from .common import (
    _DIGEST_KINDS,
    MAX_CHUNK_RANGE,
    MAX_LINES_DEFAULT,
    MAX_LINES_HARD_CAP,
    _append_participation_nudge,
    _offload,
    _regather_call,
    _requires_corpus,
    _safe_path,
    _with_freshness,
)
from .params import ChunkIdx, Corpus, CorpusFile

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _digest_kind_for_file(wg: str, file: str) -> Optional[str]:  # noqa: ARG001
    """If `file` identifies a per-corpus digest (`digests/<kind>.md`),
    return the digest `kind`; otherwise None.

    Used so chunk-fetch / file-section calls on a digest file can
    return a working hint instead of an opaque "not found" — these
    files exist but aren't in the embedding index by design.
    """
    kind = digest_kind_from_relpath(file)
    if kind is not None and kind in _DIGEST_KINDS:
        return kind
    return None


@_requires_corpus
def tool_get_chunks_batch(wg: str, requests: List[Dict[str, Any]]) -> str:
    """Fetch multiple (file, chunk_idx [, end_chunk_idx]) chunks in one
    call. Returns the concatenated chunk texts, each prefixed with its
    file + chunk-index header. Total chunks across all requests are
    capped at MAX_CHUNK_RANGE (20).

    Use this when search_corpus or read_digest returns multiple hits
    spanning different files and you want to read them all together
    rather than round-tripping per file.
    """
    # Defensive against the consumer passing a single dict instead of a
    # list — MCP serialisation can flatten unintentionally.
    if isinstance(requests, dict):
        requests = [requests]
    if not requests:
        return "(no requests)"

    total = 0
    for req in requests:
        if not isinstance(req, dict):
            return f"Each request must be an object; got {req!r}."
        try:
            start = int(req.get("chunk_idx", 0))
            end = req.get("end_chunk_idx")
            span = (int(end) - start + 1) if end is not None else 1
        except (TypeError, ValueError):
            return "chunk_idx and end_chunk_idx must be integers in request " f"{req}."
        if span < 1:
            return (
                f"end_chunk_idx must be >= chunk_idx in request "
                f"{req}; got span {span}."
            )
        total += span
    if total > MAX_CHUNK_RANGE:
        return (
            f"Requested {total} chunks total; max per call is "
            f"{MAX_CHUNK_RANGE}. Split into smaller batches."
        )

    out_parts: List[str] = []
    seen_files: List[str] = []
    for req in requests:
        file = str(req.get("file") or "")
        if not file:
            out_parts.append("_(skipped: missing file)_\n")
            continue
        seen_files.append(file)
        start = int(req.get("chunk_idx", 0))
        end = req.get("end_chunk_idx")
        end_val = int(end) if end is not None else None
        # Suppress the per-chunk footer; emit one for the whole batch below.
        single = tool_get_chunk(wg, file, start, end_chunk_idx=end_val, add_nudge=False)
        out_parts.append(f"## {file} @ chunk {start}")
        if end_val is not None:
            out_parts[-1] += f"–{end_val}"
        out_parts.append("")
        out_parts.append(single)
        out_parts.append("")
    return _append_participation_nudge(
        seen_files, _with_freshness(wg, "\n".join(out_parts))
    )


@_requires_corpus
def tool_get_by_url(wg: str, url: str) -> str:
    """Resolve a citation URL to its cached corpus content.

    Use this whenever you encounter an archive permalink in a message
    body, a footnote, or another tool's output and want the gathered
    message behind it — don't conclude "not in the corpus" from a bare
    URL without trying here first.

    Matches against the `url` column the chunker stamped at index time,
    tolerant of the incidental spelling differences a mail client adds
    (trailing slash, `http`/`https`, leading `www.`, `<...>` wrapping,
    `#fragment`). Two cases by URL kind:

    - **Thread `Archived-At:` permalink** → matches exactly one chunk
      (per-message). Returned as a single chunk. The stored form varies
      by list: most IETF lists use
      `https://mailarchive.ietf.org/arch/msg/<list>/<token>`, while some
      (e.g. httpbis) use `https://www.w3.org/mid/<message-id>`. Both
      resolve; paste whichever form you have.
    - **GitHub issue URL** → matches every chunk in the per-issue file
      (file-level URL). Returned as the file's concatenated content,
      since the consumer almost certainly wants the issue, not just
      its frontmatter header.
    - **Draft / charter URL** (file-level) → a draft's
      `https://datatracker.ietf.org/doc/<name>/` page or a charter's
      `Source:` URL. Returned as the file's concatenated content.

    One unbridged gap: a `mailarchive.ietf.org/arch/msg/<token>` link
    will not resolve against a message stored under its `www.w3.org/mid`
    form (or vice versa) — the opaque token and the Message-ID are not
    string-convertible. Same list, different identifier scheme.
    """
    matches = find_chunks_by_url(wg, url)
    if not matches:
        reindex = (
            f"re-gather it with {_regather_call(wg)}"
            if gather_enabled()
            else f"run `ietf-llm {wg} --rebuild-embeddings`"
        )
        return (
            f"No cached chunk for {url}. get_by_url resolves the URL forms "
            "stamped in the corpus: mailing-list `Archived-At:` permalinks "
            "(either `https://mailarchive.ietf.org/arch/msg/<list>/<token>` "
            "or `https://www.w3.org/mid/<message-id>`, depending on the list), "
            "GitHub issue URLs, and draft `datatracker.ietf.org/doc/<name>/` / "
            "charter `Source:` URLs. Matching tolerates trailing-slash, "
            "scheme, and `www.` differences. Two reasons a well-formed "
            "permalink still misses: the message lives in a different corpus "
            "(gather that list and retry), or the link uses the opposite "
            "identifier scheme from the one stored here (a `mailarchive` "
            "token cannot be mapped to a stored `w3.org/mid` Message-ID). If "
            f"you expected a match, the index may predate the `url` column "
            f"({reindex})."
        )
    if len(matches) == 1:
        file, chunk_idx, title, text, start_line, end_line = matches[0]
        where = f" (lines {start_line}-{end_line})" if start_line is not None else ""
        header = f"# {title}{where}\n" f"_file:_ `{file}`  ·  _chunk:_ {chunk_idx}\n\n"
        return _with_freshness(wg, header + text)
    # Multiple chunks → file-level URL. Concatenate by chunk order.
    file = matches[0][0]
    n_chunks = len(matches)
    parts: List[str] = [
        f"# {url}\n",
        f"_file:_ `{file}`  ·  _chunks:_ 0..{n_chunks - 1}\n",
        "",
    ]
    for _f, idx, title, text, _s, _e in matches:
        parts.append(f"## chunk {idx}: {title}")
        parts.append("")
        parts.append(text)
        parts.append("")
    return _with_freshness(wg, "\n".join(parts))


@_requires_corpus
def tool_get_chunk(  # pylint: disable=too-many-return-statements
    wg: str,
    file: str,
    chunk_idx: int,
    end_chunk_idx: Optional[int] = None,
    add_nudge: bool = True,
) -> str:
    # Digest files aren't chunked — point the caller at read_digest
    # instead of returning the unhelpful "Chunk not found".
    digest_kind = _digest_kind_for_file(wg, file)
    if digest_kind is not None:
        return (
            f"`{file}` is a digest, not a chunked file. "
            f"Call `read_digest(wg='{wg}', kind='{digest_kind}')` "
            "(optionally with filters) instead."
        )

    # Range fetch: return consecutive chunks in one call so consumers
    # don't have to round-trip per chunk for a small thread / issue.
    if end_chunk_idx is not None:
        if end_chunk_idx < chunk_idx:
            return (
                f"end_chunk_idx={end_chunk_idx} is less than " f"chunk_idx={chunk_idx}."
            )
        span = end_chunk_idx - chunk_idx + 1
        if span > MAX_CHUNK_RANGE:
            return (
                f"Requested {span} chunks; max per call is "
                f"{MAX_CHUNK_RANGE}. Fetch in smaller batches."
            )
        parts: List[str] = []
        any_found = False
        for idx in range(chunk_idx, end_chunk_idx + 1):
            result = get_chunk(wg, file, idx)
            if result is None:
                continue
            any_found = True
            title, text, start_line, end_line = result
            where = (
                f" (lines {start_line}-{end_line})" if start_line is not None else ""
            )
            parts.append(f"## chunk {idx}: {title}{where}\n\n{text}")
        if not any_found:
            return _chunk_not_found_hint(wg, file, chunk_idx)
        return _append_participation_nudge(
            file, "\n\n---\n\n".join(parts), enabled=add_nudge
        )

    result = get_chunk(wg, file, chunk_idx)
    if result is None:
        return _chunk_not_found_hint(wg, file, chunk_idx)
    title, text, start_line, end_line = result
    where = f" (lines {start_line}-{end_line})" if start_line is not None else ""
    return _append_participation_nudge(
        file, f"# {title}{where}\n\n{text}", enabled=add_nudge
    )


def _chunk_not_found_hint(wg: str, file: str, chunk_idx: int) -> str:
    """Compose a 'not found' message that tells the caller what's
    actually available, so they don't have to blind-probe.
    """
    counts = chunk_counts(wg)
    available = counts.get(file)
    if available is None:
        no_index = (
            f"the search index hasn't been built — re-gather it with {_regather_call(wg)}"
            if gather_enabled()
            else f"`ietf-llm {wg} --embed` hasn't been run"
        )
        return (
            f"No chunks indexed for `{file}` in {wg}. "
            "Either the file isn't in the embedding index "
            f"(check `list_files('{wg}')`), or {no_index}."
        )
    return (
        f"Chunk {chunk_idx} not found in `{file}`. "
        f"This file has {available} chunks (0..{available - 1})."
    )


@_requires_corpus
def tool_read_file_section(
    wg: str,
    file: str,
    start_line: int = 1,
    max_lines: int = MAX_LINES_DEFAULT,
) -> str:
    # Reading a digest file by line range works but is the wrong shape
    # for catalogue queries — nudge towards read_digest with filters.
    digest_kind = _digest_kind_for_file(wg, file)
    if digest_kind is not None and start_line == 1:
        # Only emit the hint on the typical "show me this file" call —
        # if the caller already passed a non-default start_line they
        # know what they're doing.
        path = _safe_path(wg, file)
        if path is None:
            return (
                f"`{file}` is a digest. Call "
                f"`read_digest(wg='{wg}', kind='{digest_kind}')` instead."
            )
        # Fall through and serve the file, but prefix the hint.
        hint = (
            f"[hint: for filtered catalogue queries use "
            f"`read_digest(wg='{wg}', kind='{digest_kind}', ...)` — "
            f"it's faster and easier on context]\n\n"
        )
        return hint + _read_section(path, start_line, max_lines)
    path = _safe_path(wg, file)
    if not path:
        return f"File not found in {wg} cache: {file}"
    return _append_participation_nudge(file, _read_section(path, start_line, max_lines))


def _read_section(path: str, start_line: int, max_lines: int) -> str:
    if max_lines > MAX_LINES_HARD_CAP:
        return (
            f"max_lines={max_lines} exceeds hard cap {MAX_LINES_HARD_CAP}. "
            "Use search() or get_chunk() instead of reading huge files."
        )
    start_line = max(1, int(start_line))
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh, 1):
            if idx < start_line:
                continue
            if idx >= start_line + max_lines:
                out.append(f"... [truncated at line {idx}; use start_line to continue]")
                break
            out.append(line.rstrip("\n"))
    return "\n".join(out)


# --- MCP server wiring -------------------------------------------------------


def register(server: "FastMCP") -> None:
    @server.tool()
    async def get_chunk_text(
        corpus: Corpus,
        file: CorpusFile,
        chunk_idx: ChunkIdx,
        end_chunk_idx: Annotated[
            Optional[int],
            Field(
                description=(
                    "Last index of a consecutive range, inclusive; at most 20 "
                    "chunks per call."
                ),
                ge=0,
            ),
        ] = None,
    ) -> str:
        """Get the full text of a chunk, or a consecutive range of them —
        typically one mailing-list message, issue comment, or draft section,
        as returned by `search_corpus`.

        Per-corpus digests (`digests/*.md`) are not chunked; use `read_digest`
        for those.
        """
        return await _offload(
            tool_get_chunk, corpus, file, chunk_idx, end_chunk_idx=end_chunk_idx
        )

    @server.tool()
    async def get_chunks_batch(
        corpus: Corpus,
        requests: Annotated[
            List[Dict[str, Any]],
            Field(
                description=(
                    "One dict per chunk or range: `file` (str), `chunk_idx` "
                    "(int), optional `end_chunk_idx` (int, inclusive). 20 "
                    "chunks total across all requests."
                )
            ),
        ],
    ) -> str:
        """Fetch chunks from several files in one round-trip, for when
        `search_corpus` returned hits across multiple files and you want them
        all without N calls.
        """
        return await _offload(tool_get_chunks_batch, corpus, requests)

    @server.tool()
    async def get_by_url(
        corpus: Corpus,
        url: Annotated[
            str,
            Field(
                description=(
                    "A `https://www.w3.org/mid/<message-id>` list permalink (the "
                    "`Archived-At:` URL, not a mailarchive.ietf.org one) or a "
                    "GitHub issue URL, exactly as it appears in the data."
                )
            ),
        ],
    ) -> str:
        """Resolve a citation URL to its cached chunk, returning the same
        shape as `get_chunk_text`. Reach for it when the user pastes such a
        URL, or a chunk cites one.

        Matching is exact against the URL stamped at index time: a link taken
        straight from a message header resolves, a hand-built archive URL will
        not.
        """
        return await _offload(tool_get_by_url, corpus, url)

    @server.tool()
    async def read_file_section(
        corpus: Corpus,
        file: CorpusFile,
        start_line: Annotated[int, Field(description="First line, 1-based.", ge=1)] = 1,
        max_lines: Annotated[
            int, Field(description="Lines to return.", ge=1, le=MAX_LINES_HARD_CAP)
        ] = MAX_LINES_DEFAULT,
    ) -> str:
        """Read a bounded section of any file in a corpus — thread and issue
        files, drafts, slides, transcripts, minutes. RFC bodies are here only
        when gathered with `--rfcs`; otherwise use `get_rfc`.

        Prefer `search_corpus` / `get_chunk_text` for very large files.
        """
        return await _offload(
            tool_read_file_section, corpus, file, start_line, max_lines
        )
