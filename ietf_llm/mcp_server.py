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

from .embeddings import _get_embed_model, get_chunk, search
from .utils import Verbosity, get_cache_dir, get_wg_file_cache_dir

MAX_LINES_DEFAULT = 400
MAX_LINES_HARD_CAP = 2000


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


def _digest_path(wg: str, kind: str) -> Optional[str]:
    if kind not in ("index", "issues", "threads"):
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


def tool_list_files(wg: str) -> str:
    cache = get_wg_file_cache_dir(wg)
    if not os.path.isdir(cache):
        return f"No cache for {wg}."
    rows = []
    for name in sorted(os.listdir(cache)):
        path = os.path.join(cache, name)
        if not os.path.isfile(path):
            continue
        rows.append(f"{os.path.getsize(path):>10}  {name}")
    return "\n".join(rows) or "(empty)"


def tool_read_digest(wg: str, kind: str = "index") -> str:
    path = _digest_path(wg, kind)
    if not path:
        return (
            f"No '{kind}' digest for {wg}. "
            f"Valid kinds: index, issues, threads. "
            f"Run `ietf-llm {wg}` to generate digests."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def tool_search(wg: str, query: str, k: int = 10) -> str:
    hits = search(wg, query, k=k, verbose=Verbosity.QUIET)
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


def tool_get_chunk(wg: str, file: str, chunk_idx: int) -> str:
    result = get_chunk(wg, file, chunk_idx)
    if result is None:
        return f"Chunk not found: {file}#{chunk_idx}"
    title, text, start_line, end_line = result
    where = (
        f" (lines {start_line}-{end_line})" if start_line is not None else ""
    )
    return f"# {title}{where}\n\n{text}"


def tool_read_file_section(
    wg: str,
    file: str,
    start_line: int = 1,
    max_lines: int = MAX_LINES_DEFAULT,
) -> str:
    path = _safe_path(wg, file)
    if not path:
        return f"File not found in {wg} cache: {file}"
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
        """List IETF Working Groups gathered locally."""
        return tool_list_working_groups()

    @server.tool()
    def list_files(wg: str) -> str:
        """List files (with sizes in bytes) in a WG's gathered cache."""
        return tool_list_files(wg)

    @server.tool()
    def read_digest(wg: str, kind: str = "index") -> str:
        """Read a digest file. kind = "index" | "issues" | "threads". Start here."""
        return tool_read_digest(wg, kind)

    @server.tool()
    def search_corpus(wg: str, query: str, k: int = 10) -> str:
        """Semantic search over a WG's gathered corpus. Returns top-k chunks
        with file, chunk_idx, title, score, snippet. Requires that
        `ietf-llm <wg> --embed` has been run.
        """
        return tool_search(wg, query, k=k)

    @server.tool()
    def get_chunk_text(wg: str, file: str, chunk_idx: int) -> str:
        """Full text of an indexed chunk returned by search_corpus."""
        return tool_get_chunk(wg, file, chunk_idx)

    @server.tool()
    def read_file_section(
        wg: str,
        file: str,
        start_line: int = 1,
        max_lines: int = MAX_LINES_DEFAULT,
    ) -> str:
        """Bounded read of any file in the WG cache. Capped at 2000 lines
        per call so the context window can't be blown by accident. Prefer
        search_corpus / get_chunk_text for large files.
        """
        return tool_read_file_section(wg, file, start_line, max_lines)

    _prewarm_embedding_model()
    server.run()


if __name__ == "__main__":
    main()
