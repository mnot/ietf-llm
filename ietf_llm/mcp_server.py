"""
MCP server for ietf-llm. Exposes the gathered corpus to MCP clients
(Claude Desktop, Claude Code, etc.) via a small set of tools focused on
context-safe retrieval.

Tools:
  list_working_groups()
      -> the WGs that have been gathered locally.
  read_digest(wg, kind="index"|"issues"|"threads")
      -> contents of one of the small digest files. Start here.
  search(wg, query, k=10)
      -> top-k semantic chunks (file, chunk_idx, title, score, snippet).
         Requires that `ietf-llm <wg> --embed` has been run.
  get_chunk(wg, file, chunk_idx)
      -> full text of a single indexed chunk (one message / one issue / one
         draft section). Use after search to read a hit in full without
         pulling the whole source file.
  read_file_section(wg, file, start_line=1, max_lines=400)
      -> bounded read of any file in the WG's cache
         (~/.cache/ietf-llm/<wg>/files/). Refuses to return more than
         `max_lines` lines (default 400) so context can't be blown by
         accident.
  list_files(wg)
      -> filenames + sizes for the WG.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import List, Optional

from .digest.query import query_digest
from .embeddings import _get_embed_model, chunk_counts, get_chunk, search
from .digest.overview import build_overview
from .utils import Verbosity, get_cache_dir, get_wg_file_cache_dir

MAX_LINES_DEFAULT = 400
MAX_LINES_HARD_CAP = 2000
# Cap on how many chunks one get_chunk_text call can return when a range
# is requested. Generous — chunks are bounded to MAX_CHUNK_CHARS (8 KB)
# each, so 20 is ~160 KB worst case but typically far less.
MAX_CHUNK_RANGE = 20


def _list_wgs() -> List[str]:
    root = get_cache_dir()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if name.startswith("_") or name.startswith("."):
            continue
        if os.path.isdir(os.path.join(root, name, "files")):
            out.append(name)
    return out


def _safe_path(wg: str, file: str) -> Optional[str]:
    """Resolve `file` inside the WG's file cache; refuse path escapes."""
    cache = get_wg_file_cache_dir(wg)
    candidate = os.path.realpath(os.path.join(cache, file))
    if not candidate.startswith(os.path.realpath(cache) + os.sep):
        return None
    if not os.path.isfile(candidate):
        return None
    return candidate


_DIGEST_KINDS = ("index", "issues", "threads", "people", "timeline")


def _digest_path(wg: str, kind: str) -> Optional[str]:
    if kind not in _DIGEST_KINDS:
        return None
    cache = get_wg_file_cache_dir(wg)
    path = os.path.join(cache, f"{wg}-_{kind}.md")
    return path if os.path.isfile(path) else None


# --- Tool implementations (plain functions, also usable for unit tests) -----


def tool_list_working_groups() -> str:
    wgs = _list_wgs()
    if not wgs:
        return "(no working groups gathered yet — run `ietf-llm <wg>`)"
    return "\n".join(wgs)


def tool_overview(wg: str) -> str:
    return build_overview(wg, get_wg_file_cache_dir(wg))


def tool_list_files(wg: str) -> str:
    cache = get_wg_file_cache_dir(wg)
    if not os.path.isdir(cache):
        return f"No cache for {wg}."
    # chunk_counts() is cheap (one GROUP BY) and lets the consumer bound
    # get_chunk_text calls instead of blind-probing chunk_idx=0,1,2,…
    counts = chunk_counts(wg)
    rows = []
    for name in sorted(os.listdir(cache)):
        path = os.path.join(cache, name)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        n_chunks = counts.get(name)
        if n_chunks is not None:
            rows.append(f"{size:>10}  chunks={n_chunks:<4}  {name}")
        elif name.startswith(f"{wg}-_") and name.endswith(".md"):
            # _-prefixed digests are intentionally NOT chunked; flag them
            # so consumers know to use read_digest, not get_chunk_text.
            kind = name[len(f"{wg}-_"):-len(".md")]
            rows.append(
                f"{size:>10}  (digest)     {name}  "
                f"-> read_digest(wg, kind='{kind}')"
            )
        else:
            rows.append(f"{size:>10}  (no chunks)  {name}")
    return "\n".join(rows) or "(empty)"


def tool_read_digest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
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
) -> str:
    path = _digest_path(wg, kind)
    if not path:
        valid = ", ".join(_DIGEST_KINDS)
        return (
            f"No '{kind}' digest for {wg}. "
            f"Valid kinds: {valid}. "
            f"Run `ietf-llm {wg}` to generate digests."
        )
    return query_digest(
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
    )


def tool_search(
    wg: str,
    query: str,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> str:
    hits = search(
        wg,
        query,
        k=k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        verbose=Verbosity.QUIET,
    )
    if not hits:
        return (
            f"(no results — has `ietf-llm {wg} --embed` been run?)"
        )
    lines = []
    for i, hit in enumerate(hits, 1):
        loc = (
            f" lines={hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else ""
        )
        lines.append(
            f"[{i}] score={hit.score:.3f}  file={hit.file}  "
            f"chunk={hit.chunk_idx}{loc}"
        )
        lines.append(f"     {hit.title}")
        lines.append(f"     {hit.snippet}")
    return "\n".join(lines)


def _digest_kind_for_file(wg: str, file: str) -> Optional[str]:
    """If `file` is one of the per-WG digests (`<wg>-_<kind>.md`),
    return the digest `kind`; otherwise None.

    Used so chunk-fetch / file-section calls on a digest file can
    return a working hint instead of an opaque "not found" — these
    files exist but aren't in the embedding index by design.
    """
    prefix = f"{wg}-_"
    if file.startswith(prefix) and file.endswith(".md"):
        kind = file[len(prefix):-len(".md")]
        if kind in _DIGEST_KINDS:
            return kind
    return None


def tool_get_chunk(  # pylint: disable=too-many-return-statements
    wg: str,
    file: str,
    chunk_idx: int,
    end_chunk_idx: Optional[int] = None,
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
                f"end_chunk_idx={end_chunk_idx} is less than "
                f"chunk_idx={chunk_idx}."
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
                f" (lines {start_line}-{end_line})"
                if start_line is not None
                else ""
            )
            parts.append(f"## chunk {idx}: {title}{where}\n\n{text}")
        if not any_found:
            return _chunk_not_found_hint(wg, file, chunk_idx)
        return "\n\n---\n\n".join(parts)

    result = get_chunk(wg, file, chunk_idx)
    if result is None:
        return _chunk_not_found_hint(wg, file, chunk_idx)
    title, text, start_line, end_line = result
    where = (
        f" (lines {start_line}-{end_line})" if start_line is not None else ""
    )
    return f"# {title}{where}\n\n{text}"


def _chunk_not_found_hint(wg: str, file: str, chunk_idx: int) -> str:
    """Compose a 'not found' message that tells the caller what's
    actually available, so they don't have to blind-probe.
    """
    counts = chunk_counts(wg)
    available = counts.get(file)
    if available is None:
        return (
            f"No chunks indexed for `{file}` in {wg}. "
            "Either the file isn't in the embedding index "
            f"(check `list_files('{wg}')`), or "
            f"`ietf-llm {wg} --embed` hasn't been run."
        )
    return (
        f"Chunk {chunk_idx} not found in `{file}`. "
        f"This file has {available} chunks (0..{available - 1})."
    )


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
    return _read_section(path, start_line, max_lines)


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
                out.append(
                    f"... [truncated at line {idx}; use start_line to continue]"
                )
                break
            out.append(line.rstrip("\n"))
    return "\n".join(out)


# --- MCP server wiring -------------------------------------------------------


def _prewarm_embedding_model() -> None:
    """Eagerly load the embedding model so the first search_corpus call
    doesn't take ~10s and look like the server has hung.

    Skips if no WG has an embedding index yet (search isn't usable anyway).
    Picks the model name from the first index found so a non-default
    --embed-model gets pre-warmed too. Errors are swallowed; pre-warm is
    best-effort and lazy loading on first call still works as a fallback.
    """
    root = get_cache_dir()
    if not os.path.isdir(root):
        return
    model_name: Optional[str] = None
    for name in sorted(os.listdir(root)):
        db_path = os.path.join(root, name, "embeddings.db")
        if not os.path.isfile(db_path):
            continue
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='model'"
                ).fetchone()
                if row:
                    model_name = row[0]
                    break
        except sqlite3.Error:
            continue
    if not model_name:
        return

    print(
        f"ietf-llm-mcp: pre-warming embedding model '{model_name}' "
        "(one-time, ~10s)...",
        file=sys.stderr,
        flush=True,
    )
    try:
        model = _get_embed_model(model_name, Verbosity.QUIET)
        if model is not None:
            # llm-sentence-transformers loads weights lazily on first
            # embed() — force that here.
            list(model.embed("warmup"))
        print("ietf-llm-mcp: ready.", file=sys.stderr, flush=True)
    except Exception as err:  # pylint: disable=broad-except
        # Best-effort: any failure here means lazy load on first
        # search_corpus call takes over. Log the exception type so the
        # symptom ("first search is slow") can be traced if needed.
        print(
            f"ietf-llm-mcp: pre-warm failed "
            f"({type(err).__name__}: {err}); first search may be slow.",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    try:
        from mcp.server.fastmcp import (  # pylint: disable=import-outside-toplevel,import-error
            FastMCP,
        )
    except ImportError:
        print(
            "The `mcp` package is missing — this should ship with "
            "ietf-llm. Try reinstalling: pipx install --force ietf-llm",
            file=sys.stderr,
        )
        sys.exit(1)

    server = FastMCP("ietf-llm")

    @server.tool()
    def list_working_groups() -> str:
        """List IETF Working Groups (WGs) gathered into the local
        ietf-llm corpus. Use this first if you don't know which
        `<wg>` shortname to pass to the other ietf-llm tools.
        """
        return tool_list_working_groups()

    @server.tool()
    def overview(wg: str) -> str:
        """IETF Working Group orientation — one call returns chairs/ADs,
        active drafts, the 5 most recently updated open issues, the 5
        most recent mailing list threads, and the latest meeting + latest
        draft publication.

        **Best first call** for any question about an IETF WG by
        shortname (`httpbis`, `quic`, `tls`, `aipref`, …) — ~30 lines
        of markdown instead of the 80-100KB of context that reading
        every digest would burn.

        Companion ietf-llm tools to call after this:
          - `read_digest(wg, kind=...)` — filtered catalogue
            (kinds: index, issues, threads, people, timeline).
            Use for "what's open?", "who's a chair?", "what happened
            in May?" — pass filters, don't read the whole digest.
          - `search_corpus(wg, query)` — semantic search across the
            mailing list, drafts, issues, slides, and transcripts.
            Use for "what was said about X?"
          - `get_chunk_text(wg, file, chunk_idx)` — full text of one
            chunk returned by `search_corpus`.
          - `read_file_section(wg, file, start_line)` — bounded read
            of any cache file.
          - `list_files(wg)` — file inventory with chunk counts.
        """
        return tool_overview(wg)

    @server.tool()
    def list_files(wg: str) -> str:
        """List files in an IETF Working Group's gathered ietf-llm cache.

        Each row shows size, chunk count (where indexed), and filename.
        `(digest)` rows are the per-WG summary digests — read them via
        `read_digest`, not `get_chunk_text`.
        """
        return tool_list_files(wg)

    @server.tool()
    def read_digest(  # pylint: disable=too-many-arguments,too-many-positional-arguments
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
    ) -> str:
        """Filtered catalogue read of an IETF Working Group's gathered
        digests (issues, mailing list threads, participants, timeline,
        index). The high-value catalogue tool — pair it with `overview`
        for "tell me about this WG"-shaped questions.

        kind = "index"    — corpus inventory + how-to-use pointer
             | "issues"   — one row per GitHub issue. Filters: state
                            ("open"/"closed"), label (substring),
                            author (substring), limit (int).
             | "threads"  — one row per mailing list thread. Filters:
                            since/until ("YYYY-MM-DD"), min_messages,
                            limit.
             | "people"   — participants. Filters: role (substring,
                            e.g. "Chair"), min_messages, limit.
             | "timeline" — chronological events. Filters: since/until,
                            event_kind ("draft-published" /
                            "issue-opened" / "issue-closed" /
                            "meeting" / "wglc" / "adoption-call"),
                            limit.

        Pass no filters to get the full digest (same bytes as before).
        Filters compose (AND); `limit` truncates after filtering.
        For catalogue-style queries (e.g. "open issues with label X"),
        always use filters rather than reading the full digest and
        scanning — both faster and easier on context.
        """
        return tool_read_digest(
            wg, kind,
            state=state, label=label, author=author, role=role,
            since=since, until=until, event_kind=event_kind,
            min_messages=min_messages, limit=limit,
        )

    @server.tool()
    def search_corpus(
        wg: str,
        query: str,
        k: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> str:
        """Semantic search across an IETF Working Group's gathered
        ietf-llm corpus — mailing list threads, GitHub issues, drafts,
        RFCs, slides, transcripts, minutes. Returns top-k chunks with
        file, chunk_idx, title, score, snippet, and line range.

        Use for substantive "what was said about X?" / "what's the WG's
        stance on Y?" questions. Pivot with `get_chunk_text` or
        `read_file_section` to read a hit in context.
        Requires `ietf-llm <wg> --embed` to have been run.

        Optional facets:
          - file_pattern: SQL LIKE pattern (e.g. "%mailing-list%" to
            restrict to the mailing list, "%github%" for GitHub issues).
            % is wildcard.
          - since / until: ISO 8601 dates (e.g. "2026-01-01"). Only
            mailing-list and GitHub chunks have dates; windowed draft
            chunks are excluded when either bound is set.
        """
        return tool_search(
            wg, query, k=k, file_pattern=file_pattern, since=since, until=until
        )

    @server.tool()
    def get_chunk_text(
        wg: str,
        file: str,
        chunk_idx: int,
        end_chunk_idx: Optional[int] = None,
    ) -> str:
        """Full text of an indexed chunk from an IETF Working Group's
        ietf-llm corpus — typically a single mailing list message,
        a GitHub issue comment, or a draft section.

        Pass `end_chunk_idx` to fetch a consecutive range in one call
        (e.g. an entire short thread). Range size is capped at
        20 chunks per call.

        Note: per-WG digests (`<wg>-_*.md`) are not chunked — use
        `read_digest` for those.
        """
        return tool_get_chunk(wg, file, chunk_idx, end_chunk_idx=end_chunk_idx)

    @server.tool()
    def read_file_section(
        wg: str,
        file: str,
        start_line: int = 1,
        max_lines: int = MAX_LINES_DEFAULT,
    ) -> str:
        """Bounded read of any file in an IETF Working Group's ietf-llm
        cache (per-thread files, per-issue files, drafts, RFCs, slides,
        transcripts, minutes). Capped at 2000 lines per call so the
        context window can't be blown by accident. Prefer
        `search_corpus` / `get_chunk_text` for very large files.
        """
        return tool_read_file_section(wg, file, start_line, max_lines)

    _prewarm_embedding_model()
    server.run()


if __name__ == "__main__":
    main()
