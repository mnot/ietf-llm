# pylint: disable=too-many-lines
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
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from .digest.overview import _label_frequencies, build_overview
from .digest.query import query_digest
from .embeddings import (
    _get_embed_model,
    chunk_counts,
    find_chunks_by_url,
    get_chunk,
    search,
)
from .freshness import staleness_warning
from .paths import digest_kind_from_relpath, digest_path
from .utils import (
    Verbosity,
    get_cache_dir,
    get_wg_file_cache_dir,
    graceful_keyboard_interrupt,
)

MAX_LINES_DEFAULT = 400
# Raised from 2000 once consumers reported hitting it on long issue
# threads. 5000 covers virtually every per-issue file in one call —
# even high-traffic issues like httpbis adoption debates cap out
# around 3-4k lines. Still a real ceiling so a runaway file can't
# blow the context window in one read.
MAX_LINES_HARD_CAP = 5000
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
    path = digest_path(cache, kind)
    return path if os.path.isfile(path) else None


# --- Tool implementations (plain functions, also usable for unit tests) -----


def _with_freshness(wg: str, body: str) -> str:
    """Prepend the staleness warning (if any) to a tool response.

    Top-level tools call this; pivot tools (get_chunk_text,
    read_file_section) skip it because the warning has already been
    seen on the call that surfaced the file in the first place.
    """
    warning = staleness_warning(wg)
    if not warning:
        return body
    return f"{warning}\n\n{body}"


def _flatten_rationale(rationale: str, limit: int) -> str:
    """Strip blockquote markers and metadata lines for a one-line
    preview of a closing rationale. The full formatted rationale lives
    in the per-issue file; this is just the inline hint in search
    output, so we want the substance of the comment, not the chrome.
    """
    cleaned: List[str] = []
    for line in rationale.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Drop the "_by Author on Date:_" italic byline and the
        # leading `> ` blockquote markers — both are formatting that
        # doesn't carry information at preview size.
        if stripped.startswith("_by ") and stripped.endswith(":_"):
            continue
        if stripped.startswith("> "):
            stripped = stripped[2:]
        cleaned.append(stripped)
    flat = " ".join(cleaned)
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat


_NEXT_TOOLS_HINT = (
    "\n\n_Next: `overview(wg)` for orientation · "
    "`read_digest(wg, kind=..., ...filters)` for catalogue queries · "
    "`search_corpus(wg, query, ...)` for substantive content · "
    "`list_labels(wg)` for the WG's curation vocabulary._"
)


def tool_list_working_groups() -> str:
    wgs = _list_wgs()
    if not wgs:
        return (
            "(no working / research groups gathered yet — "
            "run `ietf-llm <shortname>`)"
        )
    return "\n".join(wgs) + _NEXT_TOOLS_HINT


def tool_overview(wg: str) -> str:
    return _with_freshness(wg, build_overview(wg, get_wg_file_cache_dir(wg)))


def tool_list_labels(wg: str) -> str:
    """All GitHub issue labels with their frequencies, sorted by count
    descending. Direct exposure of the same data the overview's
    "Top issue labels" section samples — but here unbounded, so the
    consumer can verify the curation vocabulary before picking a
    `label=` filter for read_digest or search_corpus.
    """
    labels = _label_frequencies(get_wg_file_cache_dir(wg), wg)
    if not labels:
        return _with_freshness(
            wg, f"No labels recorded for {wg}. (No issues, or no labels used.)"
        )
    lines = [f"# {wg}: GitHub issue labels ({len(labels)} distinct)\n"]
    lines.append("| Label | Issues |")
    lines.append("|-------|--------|")
    for label, count in labels:
        lines.append(f"| `{label}` | {count} |")
    # Concrete next-call signatures so a consuming LLM doesn't have to
    # round-trip through tool_search to recall how to use a label.
    lines.append("")
    lines.append(
        f'_Next: `read_digest("{wg}", kind="issues", label="X", '
        'include_bodies=True)` for a labelled-cluster summary in one '
        f'call, or `search_corpus("{wg}", "...", label="X")` for '
        "semantic search restricted to a label._"
    )
    return _with_freshness(wg, "\n".join(lines))


def tool_list_files(wg: str, pattern: Optional[str] = None) -> str:
    cache = get_wg_file_cache_dir(wg)
    if not os.path.isdir(cache):
        return f"No cache for {wg}."
    # chunk_counts() is cheap (one GROUP BY) and lets the consumer bound
    # get_chunk_text calls instead of blind-probing chunk_idx=0,1,2,…
    counts = chunk_counts(wg)
    # If the embedding DB has no chunks at all, the index hasn't been
    # built yet — distinguish that from "this file genuinely has no
    # indexable content" so the consumer isn't misled into thinking
    # there's nothing to search.
    index_built = bool(counts)
    # `pattern` is a glob over the relative path. Lets a consumer ask
    # for `threads/*mlkem*` or `meetings/ietf125/*` instead of grepping
    # a 600-line inventory dump. Glob is matched against the relpath
    # (so `threads/*` works), with fnmatch semantics.
    import fnmatch  # pylint: disable=import-outside-toplevel
    entries = []
    for dirpath, _dirnames, filenames in os.walk(cache):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            relpath = os.path.relpath(path, cache)
            if pattern is not None and not fnmatch.fnmatch(relpath, pattern):
                continue
            entries.append((relpath, path))
    entries.sort(key=lambda kv: kv[0])
    if pattern is not None and not entries:
        return _with_freshness(
            wg,
            f"(no files match `{pattern}`. Try a broader glob, e.g. "
            "`threads/*` or `*mlkem*`.)",
        )
    rows = []
    for relpath, path in entries:
        size = os.path.getsize(path)
        n_chunks = counts.get(relpath)
        kind = digest_kind_from_relpath(relpath)
        if n_chunks is not None:
            rows.append(f"{size:>10}  chunks={n_chunks:<4}  {relpath}")
        elif kind is not None and kind in _DIGEST_KINDS:
            # Digests are intentionally NOT chunked; flag them so
            # consumers know to use read_digest, not get_chunk_text.
            rows.append(
                f"{size:>10}  (digest)     {relpath}  "
                f"-> read_digest(wg, kind='{kind}')"
            )
        else:
            # "not indexed" when the DB itself is empty (build hasn't
            # run yet); "no chunks" for the rare case of an indexed
            # corpus where this specific file produced zero chunks.
            tag = "(not indexed)" if not index_built else "(no chunks)"
            rows.append(f"{size:>10}  {tag}  {relpath}")
    body = "\n".join(rows) or "(empty)"
    body += (
        f"\n\n_Next: `read_file_section(\"{wg}\", \"<filename>\", "
        "start_line=1)` for a bounded read · "
        f'`get_chunk_text("{wg}", "<filename>", chunk_idx, end_chunk_idx)` '
        "for one (or a range of) indexed chunks._"
    )
    return _with_freshness(wg, body)


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
) -> str:
    path = _digest_path(wg, kind)
    if not path:
        valid = ", ".join(_DIGEST_KINDS)
        return (
            f"No '{kind}' digest for {wg}. "
            f"Valid kinds: {valid}. "
            f"Run `ietf-llm {wg}` to generate digests."
        )
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


def tool_search(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    wg: str,
    query: str,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    sort: Optional[str] = None,
) -> str:
    hits = search(
        wg,
        query,
        k=k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        label=label,
        state=state,
        sort=sort,
        verbose=Verbosity.QUIET,
    )
    if not hits:
        return _with_freshness(
            wg,
            f"(no results — has `ietf-llm {wg} --embed` been run?)",
        )
    lines = []
    # Result-set state summary. When every hit comes from a closed issue,
    # the answer the consumer cares about is "this debate is resolved"
    # — surfacing that once at the top stops an LLM from presenting an
    # archived debate as if it were live (and saves the per-hit `[closed]`
    # tags from being noise on a uniform result set).
    states = {h.state for h in hits if h.state}
    files_with_state = sum(1 for h in hits if h.state)
    if states and len(states) == 1 and files_with_state == len(hits):
        only_state = next(iter(states))
        lines.append(
            f"_All {len(hits)} hits are from {only_state} issues. "
            + (
                "This topic appears resolved; closed issues hold the "
                "chairs' resolution."
                if only_state == "closed"
                else "These issues are still under discussion."
            )
            + "_"
        )
        lines.append("")
    for i, hit in enumerate(hits, 1):
        loc = (
            f" lines={hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else ""
        )
        # State goes on the header line — it's a one-word signal that
        # changes how the caller should weight the hit. Labels (longer
        # and only sometimes present) get their own line below.
        state_tag = f"  [{hit.state}]" if hit.state else ""
        lines.append(
            f"[{i}] score={hit.score:.3f}  file={hit.file}  "
            f"chunk={hit.chunk_idx}{loc}{state_tag}"
        )
        lines.append(f"     {hit.title}")
        if hit.labels:
            lines.append(f"     labels: {hit.labels}")
        # Cluster signals — saves a follow-up file read when scanning
        # results. dup-of nudges the LLM to skip duplicate issues;
        # the closing-rationale preview surfaces the "why" without
        # the consumer having to open the file.
        if hit.duplicate_of is not None:
            lines.append(f"     duplicate of: #{hit.duplicate_of}")
        if hit.closing_rationale:
            preview = _flatten_rationale(hit.closing_rationale, 140)
            lines.append(f"     closing: {preview}")
        # Citation URL straight from the chunk: GitHub URL for issue
        # chunks, IETF Archived-At permalink for thread message chunks.
        # NULL for drafts/transcripts and pre-v6 indexes — silently skip.
        if hit.url:
            lines.append(f"     url: {hit.url}")
        lines.append(f"     {hit.snippet}")
    return _with_freshness(wg, "\n".join(lines))


def _digest_kind_for_file(wg: str, file: str) -> Optional[str]:  # noqa: ARG001
    """If `file` identifies a per-WG digest (`digests/<kind>.md`),
    return the digest `kind`; otherwise None.

    Used so chunk-fetch / file-section calls on a digest file can
    return a working hint instead of an opaque "not found" — these
    files exist but aren't in the embedding index by design.
    """
    kind = digest_kind_from_relpath(file)
    if kind is not None and kind in _DIGEST_KINDS:
        return kind
    return None


def tool_get_chunks_batch(
    wg: str, requests: List[Dict[str, Any]]
) -> str:
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
        start = int(req.get("chunk_idx", 0))
        end = req.get("end_chunk_idx")
        span = (int(end) - start + 1) if end is not None else 1
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
    for req in requests:
        file = str(req.get("file") or "")
        if not file:
            out_parts.append("_(skipped: missing file)_\n")
            continue
        start = int(req.get("chunk_idx", 0))
        end = req.get("end_chunk_idx")
        end_val = int(end) if end is not None else None
        single = tool_get_chunk(wg, file, start, end_chunk_idx=end_val)
        out_parts.append(f"## {file} @ chunk {start}")
        if end_val is not None:
            out_parts[-1] += f"–{end_val}"
        out_parts.append("")
        out_parts.append(single)
        out_parts.append("")
    return _with_freshness(wg, "\n".join(out_parts))


def tool_fetch_by_url(wg: str, url: str) -> str:
    """Resolve a citation URL to its cached corpus content.

    Exact-match on the `url` column the chunker stamped at index time.
    Two cases by URL kind:

    - **Thread `Archived-At:` permalink** → matches exactly one chunk
      (per-message). Returned as a single chunk.
    - **GitHub issue URL** → matches every chunk in the per-issue file
      (file-level URL). Returned as the file's concatenated content,
      since the consumer almost certainly wants the issue, not just
      its frontmatter header.
    """
    matches = find_chunks_by_url(wg, url)
    if not matches:
        return (
            f"No cached chunk for {url}. The URL may not be in this WG's "
            "corpus, or the index predates the `url` column (run "
            f"`ietf-llm {wg} --rebuild-embeddings`)."
        )
    if len(matches) == 1:
        file, chunk_idx, title, text, start_line, end_line = matches[0]
        where = (
            f" (lines {start_line}-{end_line})" if start_line is not None else ""
        )
        header = (
            f"# {title}{where}\n"
            f"_file:_ `{file}`  ·  _chunk:_ {chunk_idx}\n\n"
        )
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


@graceful_keyboard_interrupt
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
        """List IETF Working Groups (and IRTF Research Groups) gathered
        in the local ietf-llm corpus. Use this first when you don't
        know the `<wg>` shortname the user means. IRTF RGs use the
        same shortname convention (e.g. `cfrg`, `hrpc`) and are first-
        class — every other tool accepts them too.
        """
        return tool_list_working_groups()

    @server.tool()
    def overview(wg: str) -> str:
        """Orient on an IETF Working Group via ietf-llm: chairs/ADs,
        active drafts, top open issues, recent mailing list threads,
        latest meeting and latest draft publication — one call.

        **Best first call for ORIENTING / STRUCTURAL questions** about
        an IETF WG or IRTF RG by shortname (`httpbis`, `quic`, `tls`,
        `aipref`, `cfrg`, `hrpc`, …): "tell me about X", "what's X up
        to?", "who's on X?", "what is X working on?". ~30 lines of
        markdown instead of the 80-100 KB of context that reading
        every digest would burn.

        **Skip overview and go straight to the specialised tool for
        TOPICAL questions:**
          - "arguments for/against X" / "scope debate about X" →
            `search_corpus(wg, "X", label="...")` — issue labels are
            the WG's own curation.
          - "what did the WG decide about X?" / "what's the WG's
            position on X?" → `search_corpus(wg, "X", state="closed")`
            — the chairs' resolution lives in closed issues.
          - "what's open?" / "who chairs this?" / "what happened in
            May?" → `read_digest(wg, kind=..., ...filters)`.
          - "what did Alice say about X?" → `search_corpus` (semantic
            search, then pivot via `get_chunk_text` or
            `read_file_section`).

        Other ietf-llm tools: `read_digest`, `search_corpus`,
        `get_chunk_text`, `read_file_section`, `list_files`.
        """
        return tool_overview(wg)

    @server.tool()
    def list_labels(wg: str) -> str:
        """List GitHub issue labels in an IETF Working Group's ietf-llm
        corpus with frequencies. Call this before picking a `label=`
        filter for `search_corpus` or `read_digest` — labels are the
        WG's own curation vocabulary, but you can't guess them; this
        tool surfaces the actual terms in use, sorted by how many
        issues each label is attached to.
        """
        return tool_list_labels(wg)

    @server.tool()
    def list_files(wg: str, pattern: Optional[str] = None) -> str:
        """Inventory an IETF Working Group's ietf-llm cache: files with
        sizes and chunk counts.

        `pattern` is an optional glob over the relative path (fnmatch
        semantics), e.g. `"threads/*mlkem*"`, `"meetings/ietf125/*"`,
        `"issues/*/155.md"`. Use it instead of dumping the whole
        inventory when you already know roughly what you're after — a
        long-running WG can have 1000+ files.

        `(digest)` rows are the per-WG summary digests — read them via
        `read_digest`, not `get_chunk_text`.
        """
        return tool_list_files(wg, pattern=pattern)

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
        include_bodies: bool = False,
        subject: Optional[str] = None,
    ) -> str:
        """Read filtered catalogue digests of an IETF Working Group's
        ietf-llm corpus: issues, threads, people, timeline, index. The
        high-value catalogue tool — pair with `overview` for "tell me
        about this WG"-shaped questions, and use `label=` here to get
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
                            subject — high-value for WGs that don't
                            tag GitHub issues but cluster topics on
                            the list, e.g. TLS with `[mlkem]`, `[ech]`).
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
                            "doc-wglc"), limit.

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
            include_bodies=include_bodies, subject=subject,
        )

    @server.tool()
    def search_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        wg: str,
        query: str,
        k: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> str:
        """Search an IETF Working Group's ietf-llm corpus semantically
        across mailing list threads, GitHub issues, drafts, RFCs,
        slides, transcripts, and minutes. Returns top-k chunks with
        file, chunk_idx, title, score, snippet, line range, GitHub
        URL (for issue chunks), and (for issue chunks) the issue's
        GitHub labels + open/closed state.

        Use for substantive "what was said about X?" / "what's the WG's
        stance on Y?" questions. Pivot with `get_chunk_text` or
        `read_file_section` to read a hit in context.

        For topical questions ("arguments for/against X", "scope debate")
        try `label=` first — the WG's own labels (e.g. "vocabulary",
        "top-level", "ready to close") are usually better curation than
        semantic ranking alone. Pair with `kind="issues"` in `read_digest`
        to get the issue catalogue, then `search_corpus` for depth inside
        the matching issues.

        `state="closed"` narrows to resolved issues — prefer this when
        the user wants the WG's settled position rather than ongoing
        debate. `state="open"` is the inverse: only unresolved threads.

        `sort="date"` re-orders the top-k hits chronologically (oldest
        first) instead of by relevance, so a consumer reading
        top-to-bottom sees an early objection → settled-position
        arc. Combine with `file_pattern="%-issue-…-N.md"` to scope to
        one issue, or `since`/`until` for a time window. NULL-dated
        chunks (drafts, transcripts) are excluded under `sort="date"`.
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
            wg, query, k=k, file_pattern=file_pattern,
            since=since, until=until, label=label, state=state, sort=sort,
        )

    @server.tool()
    def get_chunk_text(
        wg: str,
        file: str,
        chunk_idx: int,
        end_chunk_idx: Optional[int] = None,
    ) -> str:
        """Get full text of a chunk (or a consecutive range) from an
        IETF Working Group's ietf-llm corpus — typically a single
        mailing list message, an issue comment, or a draft section,
        as returned by `search_corpus`.

        Pass `end_chunk_idx` to fetch a consecutive range in one call
        (e.g. an entire short thread). Range size is capped at
        20 chunks per call.

        Note: per-WG digests (`<wg>-_*.md`) are not chunked — use
        `read_digest` for those.
        """
        return tool_get_chunk(wg, file, chunk_idx, end_chunk_idx=end_chunk_idx)

    @server.tool()
    def get_chunks_batch(
        wg: str, requests: List[Dict[str, Any]],
    ) -> str:
        """Fetch multiple chunks from an IETF Working Group's ietf-llm
        corpus in one call. `requests` is a list of dicts, each with:
          - `file` (str): chunk's source file
          - `chunk_idx` (int): first chunk index
          - `end_chunk_idx` (int, optional): last chunk index (inclusive)
            for a range from this file

        Use when search_corpus returned hits across multiple files and
        you want all of them in one round-trip rather than N calls.
        Total chunks across all requests are capped at 20.
        """
        return tool_get_chunks_batch(wg, requests)

    @server.tool()
    def fetch_by_url(wg: str, url: str) -> str:
        """Resolve an external citation URL to its cached chunk in an
        IETF Working Group's ietf-llm corpus. Accepts:

        - GitHub issue URLs (e.g.
          `https://github.com/<owner>/<repo>/issues/<N>`)
        - IETF mail-archive permalinks (e.g.
          `https://mailarchive.ietf.org/arch/msg/<list>/<token>/`)

        Returns the chunk text — same shape as `get_chunk_text` —
        without requiring the caller to know which file or chunk_idx
        backs the URL. Use this when the user pastes (or you've
        already cited) a URL and you need the underlying content.
        """
        return tool_fetch_by_url(wg, url)

    @server.tool()
    def read_file_section(
        wg: str,
        file: str,
        start_line: int = 1,
        max_lines: int = MAX_LINES_DEFAULT,
    ) -> str:
        """Read a bounded section of any file in an IETF Working Group's
        ietf-llm cache (per-thread files, per-issue files, drafts, RFCs,
        slides, transcripts, minutes). Capped at 2000 lines per call
        so the context window can't be blown by accident. Prefer
        `search_corpus` / `get_chunk_text` for very large files.
        """
        return tool_read_file_section(wg, file, start_line, max_lines)

    _prewarm_embedding_model()
    server.run()


if __name__ == "__main__":
    main()
