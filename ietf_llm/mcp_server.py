# pylint: disable=too-many-lines
"""
MCP server for ietf-llm. Exposes the gathered corpus to MCP clients
(Claude Desktop, Claude Code, etc.) via a small set of tools focused on
context-safe retrieval.

Tools:
  list_corpora()
      -> the corpora that have been gathered locally.
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

# Cap native-math thread counts BEFORE any import that touches numpy /
# torch / sentence-transformers. Each MCP client connection spawns its
# own ietf-llm-mcp process; left unbounded, every process initialises
# OpenMP / MKL / OpenBLAS to use all physical cores. Two sessions →
# 2× cores of contending threads on the same cores → context-switch
# storms that look like hangs from the client's perspective.
#
# Per-query embedding embeds a handful of strings; single-threaded is
# plenty. The gather pipeline (which runs as a separate `ietf-llm`
# process) is unaffected because its entry point doesn't import this
# module. `setdefault` so a user with a different concurrency profile
# can override via shell env.
for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

# pylint: disable=wrong-import-position
import fnmatch
import functools
import re
import sqlite3
import sys
import threading
from importlib import resources
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import anyio  # ships with `mcp`; used to offload blocking tools off-loop

from .corpus import describe, kind_status
from .digest.overview import (
    _label_frequencies,
    _subject_prefix_frequencies,
    build_overview,
)
from .digest.query import query_digest
from .embeddings import (
    _get_embed_model,
    chunk_counts,
    find_chunks_by_url,
    get_chunk,
    get_messages,
    search,
)
from .freshness import staleness_warning
from .gather.citations import normalize_draft_name
from .paths import digest_kind_from_relpath, digest_path
from .positions import (
    extract_chair_statements,
    file_supports_tally,
    load_people_context,
    read_file_text,
    render_tally,
    tally_thread,
)
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


def tool_list_corpora() -> str:
    wgs = _list_wgs()
    if not wgs:
        return "(no corpora gathered yet — run `ietf-llm <name>`)"
    rows = []
    for wg in wgs:
        kind, status = kind_status(wg)
        tag = f"{kind} · {status}" if status else kind
        rows.append((wg, tag, describe(wg)))
    name_w = max(len(w) for w, _, _ in rows)
    tag_w = max(len(t) for _, t, _ in rows)
    lines = []
    for wg, tag, subject in rows:
        line = f"{wg.ljust(name_w)}  {tag.ljust(tag_w)}"
        if subject:
            line += f"  {subject}"
        lines.append(line.rstrip())
    return (
        "Gathered corpora (name · kind [· status] · what it's about). "
        "**kind** is `group` (a WG/RG/edwg/BoF — accepts every tool), "
        "`list` (a mailing list gathered on its own), `custom` (explicit "
        "drafts/repos or a followed author), or `synthetic` (an `x-` "
        "corpus). **status** is the group state (`active` / `concluded` "
        "/ `bof` / …) when known. The trailing text is the corpus's "
        "subject — the group name, the list followed, the tracked "
        "author — so you can tell what each one covers.\n\n"
        + "\n".join(lines)
        + _NEXT_TOOLS_HINT
    )


def tool_overview(wg: str) -> str:
    return _with_freshness(wg, build_overview(wg, get_wg_file_cache_dir(wg)))


def tool_list_labels(wg: str) -> str:
    """The WG's curation vocabulary — GitHub issue labels AND mailing-
    list subject-prefix clusters — with their frequencies, sorted by
    count descending.

    Two sources because two WG-management styles exist: issue-driven
    groups (httpbis, aipref) tag with GitHub labels; mail-driven
    groups (TLS, with `[mlkem]` / `[ech]`) cluster on the list. The
    consumer doesn't have to know which the WG uses — both render.
    """
    cache = get_wg_file_cache_dir(wg)
    labels = _label_frequencies(cache, wg)
    prefixes = _subject_prefix_frequencies(cache)
    if not labels and not prefixes:
        return _with_freshness(
            wg,
            f"No curation vocabulary recorded for {wg}. "
            "(No GitHub issue labels AND no `[xxx]`-style subject "
            "prefixes seen in mailing list traffic.)",
        )
    lines: List[str] = [f"# {wg}: curation vocabulary\n"]
    if labels:
        lines.append(f"## GitHub issue labels ({len(labels)} distinct)\n")
        lines.append("| Label | Issues |")
        lines.append("|-------|--------|")
        for label, count in labels:
            lines.append(f"| `{label}` | {count} |")
        lines.append("")
        lines.append(
            f'_Use with `read_digest("{wg}", kind="issues", '
            'label="X", include_bodies=True)` or '
            f'`search_corpus("{wg}", "...", label="X")`._'
        )
        lines.append("")
    if prefixes:
        lines.append(
            f"## Mailing list subject prefixes ({len(prefixes)} " "distinct)\n"
        )
        lines.append("| Prefix | Messages |")
        lines.append("|--------|----------|")
        for prefix, count in prefixes:
            lines.append(f"| `{prefix}` | {count} |")
        lines.append("")
        lines.append(
            f'_Use with `read_digest("{wg}", kind="threads", '
            'subject="[mlkem]")` to read every thread carrying the '
            "prefix, or with subject in `search_corpus` `file_pattern`."
            "_"
        )
        lines.append("")
    return _with_freshness(wg, "\n".join(lines))


def tool_find_citations(wg: str, draft_name: str) -> str:
    """Return every thread / issue file that cites the given draft.

    Reads `digests/citations.md` (built at gather time by
    `gather.citations.scan_citations`). Draft name is normalised the
    same way the scanner normalises matches (lowercase, version
    suffix stripped), so `draft-Foo-Bar-07` and `draft-foo-bar` both
    yield the same result.
    """
    cache = get_wg_file_cache_dir(wg)
    citations_md = digest_path(cache, "citations")
    if not os.path.isfile(citations_md):
        return _with_freshness(
            wg,
            f"No citations digest for {wg}. Either no thread / issue "
            "files reference any drafts, or this WG was gathered with "
            f"an older version. Re-run `ietf-llm {wg}` to rebuild.",
        )
    normalised = normalize_draft_name(draft_name)
    try:
        with open(citations_md, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return f"Couldn't read citations digest for {wg}."
    # Find the section for this draft. Sections are
    # `## `<draft>` (N citation(s))` followed by bullet lines.
    section_re = re.compile(
        rf"^## `{re.escape(normalised)}` \([^)]+\)\s*\n+" r"(?P<body>(?:^- .*\n?)*)",
        re.MULTILINE,
    )
    match = section_re.search(text)
    if match is None:
        return _with_freshness(
            wg,
            f"No citations recorded for `{normalised}` in {wg}. "
            "(The scanner only sees draft references in cached thread "
            'and issue files; check `list_files("'
            f'{wg}", pattern="drafts/{normalised}*")` to confirm '
            "the draft itself is in the corpus.)",
        )
    body = match.group("body").strip()
    out = [f"# Citations for `{normalised}` in {wg}\n", body]
    return _with_freshness(wg, "\n".join(out))


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
        f'\n\n_Next: `read_file_section("{wg}", "<filename>", '
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


def _render_file_grouped(hits: List[Any], limit: int) -> str:
    """Collapse a flat hit list to one row per file.

    Best (highest-scoring) chunk wins as the representative; the row
    carries the hit count so the consumer sees which threads are
    *consistently* relevant vs hit once on a stray keyword. Capped at
    `limit` files so the output stays bounded.
    """
    by_file: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for hit in hits:
        counts[hit.file] = counts.get(hit.file, 0) + 1
        prev = by_file.get(hit.file)
        if prev is None or hit.score > prev.score:
            by_file[hit.file] = hit
    # Rank files by best-chunk score; tie-break on hit count.
    ranked = sorted(
        by_file.values(),
        key=lambda h: (h.score, counts[h.file]),
        reverse=True,
    )[:limit]
    out: List[str] = []
    out.append(
        f"_{len(ranked)} files (collapsed from {len(hits)} chunks). "
        "Per-file rollup: best chunk shown; hit count = matching "
        "chunks per file._"
    )
    out.append("")
    for i, hit in enumerate(ranked, 1):
        hit_count = counts[hit.file]
        out.append(f"[{i}] score={hit.score:.3f}  hits={hit_count}  file={hit.file}")
        out.append(f"     best chunk {hit.chunk_idx}: {hit.title}")
        if hit.url:
            out.append(f"     url: {hit.url}")
        out.append(f"     {hit.snippet}")
    return "\n".join(out)


# --- read_topic --------------------------------------------------------------
#
# Cross-file chronological view for one topic. The mailing-list / GitHub
# corpus fragments a debate across many thread + issue files (subject lines
# fork, parallel issues open, replies branch). `search_corpus` is great
# for "which chunks match X" but reading 15 overlapping chunks doesn't
# reconstruct the *arc* of the debate. read_topic does:
#
#   1. semantic match against the query (widened fetch_k)
#   2. restrict to thread / issue chunks with a chunk_date (drafts and
#      transcripts have no place in a debate timeline)
#   3. top-k by relevance
#   4. optionally walk reply descendants in the same thread file
#   5. fetch full chunk text (NOT a snippet — the unit is a message)
#   6. sort merged set by chunk_date and render
#
# The output unit is a message, not a chunk: full body, attribution
# header, archived-at URL. A reading LLM gets "who said what when" with
# no follow-up tool calls.

# Matches the thread message section header so we can build the reply
# graph for include_replies. Mirrors `_THREAD_MSG_RE` in chunking.py but
# captures the parent index instead of stripping it.
_THREAD_REPLY_RE = re.compile(
    r"^### \[(\d+)\] \S+(?:\s+\S+)? — .+? \(reply to \[(\d+)\]\)$",
    re.MULTILINE,
)
# Cap on rendered messages so a runaway include_replies expansion can't
# blow the context window. Sized at 3× the default k=20, so a typical
# call stays well under and an unusually deep arc still fits.
_READ_TOPIC_MAX_MESSAGES = 60
# Upper bound on caller-provided k. Without this, k=200 widens fetch_k
# to 600 (an unbounded SQL OR-chain into get_messages), only for the
# render-cap below to discard most of it. Clamping keeps the cost
# bounded; the cap is just over `_READ_TOPIC_MAX_MESSAGES` so the
# matched-vs-reply ratio stays sensible after replies are added.
_READ_TOPIC_MAX_K = 50
# Per-message body cap. Chunks are themselves capped at MAX_CHUNK_CHARS
# (8 KB) but stitching 60 of those is ~480 KB; truncate long messages
# so total output stays bounded. 4 KB is plenty for a typical post.
_READ_TOPIC_MAX_BODY_CHARS = 4000


def _parse_reply_graph(text: str) -> Dict[int, List[int]]:
    """Walk a thread file's text once and return {parent_idx: [child_idx, ...]}.

    Message section headers are `### [N] DATE — Sender (reply to [P])`.
    Message number N corresponds to chunk_idx N in the indexed file
    (chunk 0 is the thread header). Children are listed in document
    order (= chronological order, since the file is written that way).
    """
    graph: Dict[int, List[int]] = {}
    for match in _THREAD_REPLY_RE.finditer(text):
        child = int(match.group(1))
        parent = int(match.group(2))
        graph.setdefault(parent, []).append(child)
    return graph


def _descendants(graph: Dict[int, List[int]], root: int) -> List[int]:
    """All transitive children of `root` in the reply graph, BFS order."""
    out: List[int] = []
    queue = list(graph.get(root, []))
    seen = set(queue)
    while queue:
        node = queue.pop(0)
        out.append(node)
        for child in graph.get(node, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return out


def _read_thread_file_text(wg: str, file: str) -> Optional[str]:
    """Read a thread file's raw text. Returns None if the file isn't in
    the WG cache (so include_replies degrades gracefully — we just skip
    the expansion rather than erroring the whole call)."""
    path = _safe_path(wg, file)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def tool_read_topic(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    wg: str,
    query: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file_pattern: Optional[str] = None,
    k: int = 20,
    include_replies: bool = False,
) -> str:
    # Clamp k before widening, so a misuse (k=500) doesn't generate a
    # 1500-row SQL OR-chain that gets thrown away by the render cap.
    if k > _READ_TOPIC_MAX_K:
        k = _READ_TOPIC_MAX_K
    elif k < 1:
        k = 1
    # Widen the fetch so we have enough material to filter to dated
    # thread/issue chunks and still hit k. 3× covers most reasonable
    # WGs; the floor of 60 stops k=5 calls from over-narrowing.
    fetch_k = max(k * 3, 60)
    hits = search(
        wg,
        query,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        # sort="date" both excludes undated chunks (drafts, transcripts,
        # thread header chunks) and orders the survivors chronologically.
        # We re-sort after merging replies anyway, but the undated-filter
        # is load-bearing — it's the only way to drop the thread header
        # chunk (chunk_idx=0) from the relevance shortlist.
        sort="date",
        verbose=Verbosity.QUIET,
    )
    if not hits:
        return _with_freshness(
            wg,
            f"(no results for {query!r} — has `ietf-llm {wg} --embed` " "been run?)",
        )

    # Keep only chunks from thread/issue files that have a date — those
    # are the only chunks that represent a "message" in a debate.
    # Windowed draft / transcript chunks may match a query but they
    # aren't messages, so the chronological view skips them.
    matched: List[Any] = []
    for hit in hits:
        if not hit.file.lower().startswith(("threads/", "issues/")):
            continue
        matched.append(hit)
        if len(matched) >= k:
            break
    if not matched:
        return _with_freshness(
            wg,
            f"(no thread / issue messages match {query!r}. "
            "Try `search_corpus` to see whether the topic lives in "
            "drafts or transcripts instead.)",
        )

    # Build the merged (file, chunk_idx) set: matched chunks plus, if
    # requested, every reply descendant in the same thread file. Issue
    # files are linear (no reply-to nesting) so include_replies is a
    # no-op for them — documented in the tool's docstring.
    matched_keys: set[Tuple[str, int]] = {(h.file, h.chunk_idx) for h in matched}
    reply_keys: set[Tuple[str, int]] = set()
    if include_replies:
        # One graph per thread file, parsed once.
        graphs: Dict[str, Dict[int, List[int]]] = {}
        for hit in matched:
            if not hit.file.lower().startswith("threads/"):
                continue
            if hit.file not in graphs:
                text = _read_thread_file_text(wg, hit.file)
                graphs[hit.file] = _parse_reply_graph(text) if text else {}
            for child in _descendants(graphs[hit.file], hit.chunk_idx):
                key = (hit.file, child)
                if key not in matched_keys:
                    reply_keys.add(key)

    all_keys = matched_keys | reply_keys
    # Cap total messages — a deeply-replied matched message can pull in
    # dozens of descendants. Render the most-recent N if we exceed the
    # cap (matched messages are guaranteed in; replies are dropped
    # oldest-first since the chair's arc-closing post is typically late).
    detail = get_messages(wg, all_keys)
    rows: List[Tuple[str, str, int, str, str, Optional[str], bool]] = []
    # Tuple: (date_iso, file, chunk_idx, title, text, url, is_matched)
    for key, vals in detail.items():
        title, text, chunk_date, url = vals
        if chunk_date is None:
            # Defensive: matched set already filtered for chunk_date in
            # search results, but include_replies pulls by chunk_idx so
            # a parent header (chunk 0) could sneak in. Skip undated.
            continue
        rows.append(
            (
                chunk_date,
                key[0],
                key[1],
                title,
                text,
                url,
                key in matched_keys,
            )
        )
    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    if len(rows) > _READ_TOPIC_MAX_MESSAGES:
        # Drop replies (not matched) from the OLD end first, preserving
        # all matched messages and the most-recent replies — those
        # carry the resolution and the arc's conclusion.
        keep_matched = [r for r in rows if r[6]]
        replies = [r for r in rows if not r[6]]
        budget = _READ_TOPIC_MAX_MESSAGES - len(keep_matched)
        if budget < 0:
            # k > _READ_TOPIC_MAX_MESSAGES: keep the most recent matched
            # set rather than truncating arbitrarily.
            rows = keep_matched[-_READ_TOPIC_MAX_MESSAGES:]
            truncated_note = (
                f"_(capped at {_READ_TOPIC_MAX_MESSAGES} most-recent "
                f"matched messages; full set had {len(keep_matched)}.)_"
            )
        else:
            keep_replies = replies[-budget:] if budget > 0 else []
            rows = sorted(
                keep_matched + keep_replies,
                key=lambda r: (r[0], r[1], r[2]),
            )
            truncated_note = (
                f"_(capped at {_READ_TOPIC_MAX_MESSAGES} messages; "
                f"dropped {len(replies) - len(keep_replies)} older "
                "reply-only message(s) to fit.)_"
            )
    else:
        truncated_note = ""

    n_matched = sum(1 for r in rows if r[6])
    n_replies = len(rows) - n_matched
    files = sorted({r[1] for r in rows})
    out: List[str] = []
    out.append(f"# Topic timeline: {query!r} in {wg}\n")
    summary = (
        f"_{len(rows)} message(s) across {len(files)} file(s), "
        f"oldest first. {n_matched} matched the query"
    )
    if include_replies:
        summary += f"; {n_replies} pulled in as reply descendants"
    summary += "._"
    out.append(summary)
    if truncated_note:
        out.append(truncated_note)
    out.append("")

    for row in rows:
        chunk_date, file, chunk_idx, title, text, url, is_matched = row
        tag = "matched" if is_matched else "reply"
        out.append("---")
        out.append("")
        out.append(f"## [{tag}] {title}")
        meta_bits = [f"_file:_ `{file}`", f"_chunk:_ {chunk_idx}"]
        if url:
            meta_bits.append(f"_url:_ {url}")
        out.append("  ·  ".join(meta_bits))
        out.append("")
        body = text.strip()
        if len(body) > _READ_TOPIC_MAX_BODY_CHARS:
            body = body[: _READ_TOPIC_MAX_BODY_CHARS - 1] + "…"
            out.append(body)
            out.append("")
            out.append(
                f"_[message truncated at {_READ_TOPIC_MAX_BODY_CHARS} "
                f"chars; full body: `get_chunk_text({wg!r}, {file!r}, "
                f"{chunk_idx})`]_"
            )
        else:
            out.append(body)
        out.append("")

    return _with_freshness(wg, "\n".join(out))


def tool_find_replies(
    wg: str,
    file: str,
    chunk_idx: int,
    max_messages: int = 20,
) -> str:
    """Return every transitive reply to a specific thread message,
    in chronological order, with full bodies.

    Surfaces "did anyone refute this?"-shaped questions: when an
    assertion lands in message [N], you want the responses to N, not
    a semantic search that scatters across the WG. Walks the reply
    graph in the same file (no cross-file replies — a true reply
    lives in the same thread by construction).
    """
    if not file.lower().startswith("threads/"):
        return (
            f"`{file}` is not a thread file. find_replies walks "
            "(reply to [N]) markers, which only thread files carry. "
            "For an issue file (`issues/…/N.md`), comments are linear: "
            "use `get_chunk_text(end_chunk_idx=...)` to read comments "
            f"after chunk {chunk_idx}."
        )
    text = _read_thread_file_text(wg, file)
    if text is None:
        return f"File not found in {wg} cache: `{file}`"
    graph = _parse_reply_graph(text)
    descendants = _descendants(graph, chunk_idx)
    if not descendants:
        return (
            f"No replies to chunk {chunk_idx} in `{file}`. Either the "
            "message has no follow-ups in this thread, or replies "
            "went to a different thread (split subject lines, "
            "cross-posts, etc.) — try `read_topic` to span threads."
        )
    keys = [(file, idx) for idx in descendants]
    detail = get_messages(wg, keys)
    rows: List[Tuple[str, int, str, str, Optional[str]]] = []
    for idx in descendants:
        info = detail.get((file, idx))
        if info is None:
            continue
        title, body, chunk_date, url = info
        rows.append((chunk_date or "", idx, title, body, url))
    rows.sort(key=lambda r: (r[0], r[1]))
    if 0 < max_messages < len(rows):
        kept = rows[:max_messages]
        truncated_note = (
            f"_(showing {max_messages} of {len(rows)} total descendants; "
            f"raise `max_messages` to see more.)_"
        )
    else:
        kept = rows
        truncated_note = ""
    out: List[str] = []
    out.append(f"# Replies to chunk {chunk_idx} in `{file}`\n")
    out.append(
        f"_{len(rows)} transitive descendant(s) in the reply graph, "
        "oldest first. Each row is the full message body — quoted "
        "blocks already elided at gather time._"
    )
    if truncated_note:
        out.append(truncated_note)
    out.append("")
    for _date, idx, title, body, url in kept:
        out.append("---")
        out.append("")
        out.append(f"## {title}  [chunk {idx}]")
        if url:
            out.append(f"_url:_ {url}")
        out.append("")
        body = body.strip()
        if len(body) > _READ_TOPIC_MAX_BODY_CHARS:
            body = body[: _READ_TOPIC_MAX_BODY_CHARS - 1] + "…"
            out.append(body)
            out.append("")
            out.append(
                f"_[message truncated; full body: "
                f"`get_chunk_text({wg!r}, {file!r}, {idx})`]_"
            )
        else:
            out.append(body)
        out.append("")
    return _with_freshness(wg, "\n".join(out))


def tool_tally_positions(wg: str, file: str) -> str:
    """Heuristic position tally for one thread / issue file.

    Counts canonical position phrasings (`+1`, `-1`, `I support`,
    `I object`, `LGTM`, conditional support, `DISCUSS`) per message
    author, with the matching excerpt and a coverage percentage so
    the consumer can see what fraction of the discussion the
    heuristic actually classified. Enriches each row with role and
    affiliation from the people digest when known — exposing the
    implementer-clustering signal alongside the raw count.
    """
    cache_dir = get_wg_file_cache_dir(wg)
    if not file_supports_tally(file):
        return (
            f"`{file}` doesn't have the per-message section structure "
            "tally_positions needs. Pass a thread file "
            "(`threads/<date>-<slug>.md`) or issue file "
            "(`issues/<owner>-<repo>/<N>.md`) instead."
        )
    text = read_file_text(cache_dir, file)
    if text is None:
        return f"File not found in {wg} cache: `{file}`"
    positions, summary = tally_thread(text)
    if not positions:
        return (
            f"No messages found in `{file}`. The file may be empty or "
            "malformed; check with `read_file_section`."
        )
    role_lookup, aff_lookup = load_people_context(cache_dir)
    # Chair statements answer the consumer's load-bearing question
    # — "is the consensus the chair declared actually visible in the
    # traffic?" — by surfacing the chair's procedural messages at
    # the top of the tally output, before the per-author counts.
    chair_statements = extract_chair_statements(text, role_lookup)
    body = render_tally(
        file,
        positions,
        summary,
        role_lookup,
        aff_lookup,
        chair_statements,
    )
    return _with_freshness(wg, body)


def tool_search(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    wg: str,
    query: str,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    sort: Optional[str] = None,
    group_by: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    snippet_chars: Optional[int] = None,
) -> str:
    # When the consumer is asking a breadth question ("which threads
    # discuss X?"), the default per-chunk hit list shows the same
    # thread five times — wasting context. group_by="file" collapses
    # to one row per file with hit count + best chunk; the per-chunk
    # view stays the default for depth questions.
    fetch_k = k if group_by != "file" else max(k * 4, 20)
    hits = search(
        wg,
        query,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        label=label,
        state=state,
        sort=sort,
        author=author,
        role=role,
        snippet_chars=snippet_chars,
        verbose=Verbosity.QUIET,
    )
    if not hits:
        return _with_freshness(
            wg,
            f"(no results — has `ietf-llm {wg} --embed` been run?)",
        )
    if group_by == "file":
        return _with_freshness(wg, _render_file_grouped(hits, k))
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
        return "\n\n---\n\n".join(parts)

    result = get_chunk(wg, file, chunk_idx)
    if result is None:
        return _chunk_not_found_hint(wg, file, chunk_idx)
    title, text, start_line, end_line = result
    where = f" (lines {start_line}-{end_line})" if start_line is not None else ""
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
                out.append(f"... [truncated at line {idx}; use start_line to continue]")
                break
            out.append(line.rstrip("\n"))
    return "\n".join(out)


# --- MCP server wiring -------------------------------------------------------


def _prewarm_embedding_model_async() -> None:
    """Kick off embedding-model pre-warming in a background daemon
    thread. Returns immediately so the MCP server can register and
    accept tool calls without blocking on a ~10s sentence-transformers
    load (Claude startup felt like a hang otherwise).

    If a search_corpus call arrives before the prewarm finishes, the
    lazy load in `_get_embed_model` runs synchronously on the search
    thread — same total latency, paid by the call that needs it.
    The `_MODEL_LOAD_LOCK` in models.py serialises the two paths so
    we don't load twice.
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
            # Busy timeout so this read waits out a concurrent gather
            # write instead of erroring with "database is locked".
            with sqlite3.connect(db_path, timeout=30.0) as conn:
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

    def _worker() -> None:
        try:
            model = _get_embed_model(model_name, Verbosity.QUIET)
            if model is not None:
                # llm-sentence-transformers loads weights lazily on
                # first embed() — force that here.
                list(model.embed("warmup"))
        except Exception:  # pylint: disable=broad-except
            # Best-effort: any failure here means lazy load on the
            # first search_corpus call takes over. Stay silent — we're
            # in the background; the search-path error log will fire
            # if loading is genuinely broken.
            pass

    threading.Thread(
        target=_worker,
        name="ietf-llm-prewarm",
        daemon=True,
    ).start()


def _load_server_instructions() -> Optional[str]:
    """Read the bundled SKILL.md and return its body (frontmatter stripped).

    Passed to FastMCP as the server-level `instructions` field, which
    MCP-compliant clients surface to the model as system-prompt
    context. Source-of-truth-once: this is the same file
    `--install-claude-skill` copies into Claude Code, so non-Claude
    harnesses (Codex, Gemini, Cursor, Zed, opencode, …) see the same
    routing rules and IETF norms without us maintaining a parallel
    guidance string.

    YAML frontmatter (the `---` block at the top with `name:` /
    `description:`) is stripped — it's skill metadata, not guidance.
    Returns None if the file is missing (shouldn't happen for an
    installed package, but a defensive None lets the server come up
    anyway).
    """
    try:
        skill_path = resources.files("ietf_llm").joinpath("data/skill/SKILL.md")
        text = skill_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    # Strip a leading YAML frontmatter block (--- ... ---). The skill
    # always starts with one; tolerate its absence to keep the helper
    # robust to future edits.
    stripped = text.lstrip()
    if stripped.startswith("---"):
        end_marker = stripped.find("\n---", 3)
        if end_marker != -1:
            body_start = stripped.find("\n", end_marker + 4)
            if body_start != -1:
                return stripped[body_start + 1 :].lstrip()
    return text


async def _offload(fn: Callable[..., str], *args: Any, **kwargs: Any) -> str:
    """Run a blocking `tool_*` function in a worker thread.

    FastMCP invokes sync tool functions directly on the asyncio event
    loop, so any blocking work (embedding-model load, a numpy matmul
    over every chunk, a large file read, a heavy tally) freezes the
    whole server for its duration — it can't read stdin or answer
    other requests, which the client experiences as a hang/timeout.
    Registering each tool as `async def` that awaits this helper keeps
    the loop responsive: the blocking body runs off-loop, and even
    GIL-bound Python work yields between handoffs so the protocol
    stays alive.

    `abandon_on_cancel=True`: if the client cancels (its own tool
    timeout fired, say), stop waiting immediately rather than blocking
    the loop until the thread finishes; the thread completes and frees
    its slot on its own.
    """
    # run_sync loses the return type through functools.partial; the
    # tool_* functions all return str, so cast.
    return cast(
        str,
        await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs),
            abandon_on_cancel=True,
        ),
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

    # `instructions` is the MCP-spec mechanism for server-level
    # guidance: clients SHOULD surface it as system-prompt context.
    # Loading SKILL.md here makes the same guidance Claude Code reads
    # from the installed skill available to Codex / Gemini / Cursor /
    # Zed / opencode — one source of truth, no parallel maintenance.
    server_instructions = _load_server_instructions()
    server = FastMCP("ietf-llm", instructions=server_instructions)

    @server.tool()
    async def list_corpora() -> str:
        """List the corpora gathered locally by ietf-llm, each tagged with
        its **kind** and **status**. Use this first when you don't know
        the `corpus` name the user means.

        A corpus is whatever someone gathered. Most are IETF Working
        Groups / IRTF Research Groups by shortname (`httpbis`, `cfrg`,
        …), but a corpus can also be a standalone mailing list (`list`,
        e.g. `last-call`), an explicit draft/repo set (`custom`), or a
        synthetic `x-` corpus. **Every tool here takes any kind** — the
        `corpus` argument is the corpus name, not specifically a WG.
        `status` flags group state (`active` / `concluded` / `bof`), so
        you can tell a wound-down WG or finished BoF at a glance. Each row
        also carries the corpus's **subject** — the group's name, the
        mailing list it follows, or the author it tracks — so you can see
        what a corpus covers without opening it.
        """
        return await _offload(tool_list_corpora)

    @server.tool()
    async def overview(corpus: str) -> str:
        """Orient on a corpus via ietf-llm: chairs/ADs,
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
            `search_corpus(corpus, "X", label="...")` — issue labels are
            the WG's own curation.
          - "what did the WG decide about X?" / "what's the WG's
            position on X?" → `search_corpus(corpus, "X", state="closed")`
            — the chairs' resolution lives in closed issues.
          - "what's open?" / "who chairs this?" / "what happened in
            May?" → `read_digest(corpus, kind=..., ...filters)`.
          - "what did Alice say about X?" → `search_corpus` (semantic
            search, then pivot via `get_chunk_text` or
            `read_file_section`).
          - "how did the debate on X evolve?" / "walk me through the
            discussion of Y, chronologically" → `read_topic(corpus, "X")`.
            Returns full messages (not snippets) across threads and
            issues in date order; add `include_replies=True` for
            sub-thread descendants.

        Other ietf-llm tools: `read_digest`, `search_corpus`,
        `read_topic`, `get_chunk_text`, `read_file_section`,
        `list_files`, `list_labels`.
        """
        return await _offload(tool_overview, corpus)

    @server.tool()
    async def list_labels(corpus: str) -> str:
        """List the WG's curation vocabulary — GitHub issue labels
        AND mailing-list `[xxx]`-style subject prefixes — with
        frequencies. Call this before picking a `label=` filter for
        `read_digest` / `search_corpus`, or a `subject="[xxx]"`
        filter for `read_digest(kind="threads")`.

        Two sections because IETF WGs split by management style:
        - **GitHub issue labels** — used by issue-driven groups
          (`httpbis`, `aipref`).
        - **Mailing list subject prefixes** — used by mail-driven
          groups (`tls` with `[mlkem]` / `[ech]`).

        A WG may have one, the other, or both. The empty case
        (neither) is rare and gets a clear "no vocabulary" message.
        """
        return await _offload(tool_list_labels, corpus)

    @server.tool()
    async def find_citations(corpus: str, draft_name: str) -> str:
        """Find every thread / issue that cites a given Internet-Draft.

        The gather step scans per-thread and per-issue markdown files
        for `draft-...` references and records them in
        `digests/citations.md`. This tool reads that digest for the
        given draft name and returns each citing file plus the
        chunk_idx and a short context excerpt.

        Use when:
          - Reading a draft and wanting the surrounding list discussion
            ("what threads engage with this draft?").
          - Reading a thread that mentions a draft and wanting to find
            the *other* threads that engage the same draft.
          - Triaging "is this draft actually being discussed in the WG"
            from a count alone (overview's Documents section shows the
            count inline; this tool drills into the locations).

        `draft_name` accepts any of `draft-foo-bar`, `draft-foo-bar-07`,
        `draft-foo-bar.txt` — version suffix stripped before lookup.
        """
        return await _offload(tool_find_citations, corpus, draft_name)

    @server.tool()
    async def list_files(corpus: str, pattern: Optional[str] = None) -> str:
        """Inventory a corpus's ietf-llm cache: files with
        sizes and chunk counts.

        `pattern` is an optional glob over the relative path (fnmatch
        semantics), e.g. `"threads/*mlkem*"`, `"meetings/ietf125/*"`,
        `"issues/*/155.md"`. Use it instead of dumping the whole
        inventory when you already know roughly what you're after — a
        long-running WG can have 1000+ files.

        `(digest)` rows are the per-WG summary digests — read them via
        `read_digest`, not `get_chunk_text`.
        """
        return await _offload(tool_list_files, corpus, pattern=pattern)

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
    ) -> str:
        """Read filtered catalogue digests of a corpus:
        issues, threads, people, timeline, index. The
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
        )

    @server.tool()
    async def search_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        query: str,
        k: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        sort: Optional[str] = None,
        group_by: Optional[str] = None,
        author: Optional[str] = None,
        role: Optional[str] = None,
        snippet_chars: Optional[int] = None,
    ) -> str:
        """Search a corpus semantically
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

        `group_by="file"` collapses the per-chunk hit list to one row
        per file with a hit count, so a breadth question ("which
        threads discuss X?") returns four distinct threads instead of
        fifteen overlapping chunks. Use this when triaging WHERE a
        topic lives; switch back to the default per-chunk view for
        depth questions ("what did Alice say about Y?").

        `author="<substring>"` filters to chunks whose section header
        contains that name — "what did Rescorla say about X?" /
        "show me Mattsson's posts on Y" without needing the file path.
        Matches substrings, so partial / surname-only queries work.
        Windowed draft / transcript chunks have no author and drop out.

        `role="Chair"` (or `"Author"`, `"Editor"`, `"AD"`) filters to
        messages from people with that structural role. Useful for
        "what did the chairs decide" / "did the editor weigh in" —
        the registry stamps `(Role)` into each section header at
        gather time, and the filter matches against that tag.

        `snippet_chars=N` raises the snippet budget per hit. Default
        renders compact snippets that often `[truncated]` for long
        chunks; raise for long-form synthesis where the snippet
        itself should carry more context. Tradeoff: bigger budget
        means more bytes per hit, so dial `k` down accordingly.

        Requires the embedding index (built by default on gather;
        skipped only with `--no-embed`).

        Optional facets:
          - file_pattern: SQL LIKE pattern (e.g. "%mailing-list%" to
            restrict to the mailing list, "%github%" for GitHub issues).
            % is wildcard.
          - since / until: ISO 8601 dates (e.g. "2026-01-01"). Only
            mailing-list and GitHub chunks have dates; windowed draft
            chunks are excluded when either bound is set.
        """
        return await _offload(
            tool_search,
            corpus,
            query,
            k=k,
            file_pattern=file_pattern,
            since=since,
            until=until,
            label=label,
            state=state,
            sort=sort,
            group_by=group_by,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
        )

    @server.tool()
    async def find_replies(
        corpus: str,
        file: str,
        chunk_idx: int,
        max_messages: int = 20,
    ) -> str:
        """Return every transitive reply to a specific thread message,
        in chronological order, with full bodies.

        Use when an assertion or claim lands in a message and you want
        to know whether it was challenged, refuted, or extended in the
        same thread. The reply graph (built from `(reply to [N])`
        markers in the thread file) is walked transitively — children,
        grandchildren, and beyond — and each descendant is returned
        as a full message body, not a snippet.

        Companion to `read_topic(include_replies=True)`:
          - `read_topic` starts from a *query*, anchors on matched
            messages, optionally pulls their replies.
          - `find_replies` starts from a specific *message*. Use this
            when you know which post you want responses to.

        Thread files only — issue comments are linear, so for an
        issue file `get_chunk_text(end_chunk_idx=...)` is the right
        call to read comments after a given index.

        Bounded at 20 messages by default; raise `max_messages` for
        deep sub-threads. Bodies over 4 KB are truncated with a
        pointer to `get_chunk_text` for the full text.
        """
        return await _offload(
            tool_find_replies, corpus, file, chunk_idx, max_messages=max_messages
        )

    @server.tool()
    async def tally_positions(corpus: str, file: str) -> str:
        """Count stated positions (`+1`, `-1`, `I support`, `I object`,
        `LGTM`, conditional support, `DISCUSS`) per message author in
        ONE thread or issue file. Output also includes a **Chair
        statements** section at the top: any message from a chair
        containing procedural language (`rough consensus`,
        `consensus call`, `WGLC`, `adopting`, `closing this thread`,
        …) rendered prominently with an excerpt. The per-author
        tally + chair statements together let you ground-truth the
        chair's declared outcome against the actual list traffic.

        Coverage percentage tells you what fraction of messages the
        heuristic could classify — read it before quoting the count.

        Use this BEFORE relaying a chair's characterisation of "levels
        of support" — chair summaries are themselves sometimes the
        subject of procedural dispute (see appeals at WGLC), and the
        binding signal in IETF is the actual list traffic. A grounded
        count beats a relayed claim.

        Pass `file` as a relative path under the WG cache, e.g.
        `threads/2026-04-12-wglc-mlkem.md` or
        `issues/org-repo/155.md`. Files outside threads/ and issues/
        don't have the per-message section structure this tool reads
        and will be politely refused.

        Heuristic limitations:
          - Subtle, technical-only objections show as no-position
            (the heuristic looks for canonical phrasings, not
            sentiment).
          - Quoted text is stripped, so a `+1` quoted in someone
            else's reply doesn't double-count.
          - Bare `LGTM` / `+1` count as full support; conditional
            phrasings (`support with…`, `agree but…`) get their own
            bucket so a tally doesn't conflate yes with yes-if.

        For the *narrative* arc of a debate (full messages,
        chronological), use `read_topic`. For *catalogue* views of
        many issues at once, use `read_digest(kind="issues")`. This
        tool is the counter — one file, one tally, grounded.
        """
        return await _offload(tool_tally_positions, corpus, file)

    @server.tool()
    async def read_topic(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        query: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        file_pattern: Optional[str] = None,
        k: int = 20,
        include_replies: bool = False,
    ) -> str:
        """Read a corpus's debate as a chronological narrative
        across mailing list threads and GitHub issues. Returns the full
        text of every matched message — author, date, role, archived-at
        URL, body — in date order, oldest first.

        Use this when the user wants the **arc** of a debate, not the
        ranked hits. The unit is a message, not a chunk: each matched
        thread message or issue comment appears in full, so you get
        "who said what when" without N follow-up `get_chunk_text` calls.

        Best fit for "how did the debate on X evolve?", "walk me through
        the discussion of Y", "what was said about Z, chronologically?"
        — anything where the *direction* of the conversation matters.
        For "which threads discuss X?" use `search_corpus(group_by="file")`;
        for "what did the chairs decide?" use
        `search_corpus(state="closed")`.

        Mailing-list threads and GitHub issues only — windowed draft /
        transcript chunks are excluded since they aren't "messages" in
        a debate. The output is capped at 60 messages total; if the
        cap fires, the response says so.

        `include_replies=True` walks the reply graph in each matched
        thread file and pulls every transitive reply descendant of a
        matched message — even if those replies don't themselves match
        the query. Faithfully reconstructs sub-threads, but can
        drag in tangents; off by default. GitHub issue files are
        linear (no reply-to nesting), so `include_replies` is a no-op
        there.

        Filters compose with the semantic match:
          - `since` / `until` (ISO dates): time-window the candidates
          - `file_pattern` (SQL LIKE on the relative path): scope to
            one issue (`issues/org-repo/155.md`) or one thread cluster
            (`threads/2026-04-%mlkem%`)
          - `k`: how many top-relevance messages to anchor on (default
            20; replies expand this further). The fetch is widened
            internally so the candidate pool is roomy.

        Requires the embedding index (built by default on gather;
        skipped only with `--no-embed`).
        """
        return await _offload(
            tool_read_topic,
            corpus,
            query,
            since=since,
            until=until,
            file_pattern=file_pattern,
            k=k,
            include_replies=include_replies,
        )

    @server.tool()
    async def get_chunk_text(
        corpus: str,
        file: str,
        chunk_idx: int,
        end_chunk_idx: Optional[int] = None,
    ) -> str:
        """Get full text of a chunk (or a consecutive range) from a
        corpus — typically a single
        mailing list message, an issue comment, or a draft section,
        as returned by `search_corpus`.

        Pass `end_chunk_idx` to fetch a consecutive range in one call
        (e.g. an entire short thread). Range size is capped at
        20 chunks per call.

        Note: per-WG digests (`digests/*.md`) are not chunked — use
        `read_digest` for those.
        """
        return await _offload(
            tool_get_chunk, corpus, file, chunk_idx, end_chunk_idx=end_chunk_idx
        )

    @server.tool()
    async def get_chunks_batch(
        corpus: str,
        requests: List[Dict[str, Any]],
    ) -> str:
        """Fetch multiple chunks from a corpus in one call.
        `requests` is a list of dicts, each with:
          - `file` (str): chunk's source file
          - `chunk_idx` (int): first chunk index
          - `end_chunk_idx` (int, optional): last chunk index (inclusive)
            for a range from this file

        Use when search_corpus returned hits across multiple files and
        you want all of them in one round-trip rather than N calls.
        Total chunks across all requests are capped at 20.
        """
        return await _offload(tool_get_chunks_batch, corpus, requests)

    @server.tool()
    async def fetch_by_url(corpus: str, url: str) -> str:
        """Resolve an external citation URL to its cached chunk in a
        corpus. Accepts:

        - GitHub issue URLs (e.g.
          `https://github.com/<owner>/<repo>/issues/<N>`)
        - IETF mail-archive permalinks (e.g.
          `https://mailarchive.ietf.org/arch/msg/<list>/<token>/`)

        Returns the chunk text — same shape as `get_chunk_text` —
        without requiring the caller to know which file or chunk_idx
        backs the URL. Use this when the user pastes (or you've
        already cited) a URL and you need the underlying content.
        """
        return await _offload(tool_fetch_by_url, corpus, url)

    @server.tool()
    async def read_file_section(
        corpus: str,
        file: str,
        start_line: int = 1,
        max_lines: int = MAX_LINES_DEFAULT,
    ) -> str:
        """Read a bounded section of any file in a corpus's
        ietf-llm cache (per-thread files, per-issue files, drafts, RFCs,
        slides, transcripts, minutes). Default 400 lines per call; the
        caller can raise `max_lines` up to a hard cap of 5000 so the
        context window can't be blown by accident. Prefer
        `search_corpus` / `get_chunk_text` for very large files.
        """
        return await _offload(tool_read_file_section, corpus, file, start_line, max_lines)

    _prewarm_embedding_model_async()
    server.run()


if __name__ == "__main__":
    main()
