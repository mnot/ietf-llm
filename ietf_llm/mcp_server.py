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
      -> bounded read of any file in the corpus's cache
         (~/.cache/ietf-llm/<wg>/files/). Refuses to return more than
         `max_lines` lines (default 400) so context can't be blown by
         accident.
  list_files(wg)
      -> filenames + sizes for the corpus.
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
import datetime
import fnmatch
import functools
import json
import re
import sqlite3
import sys
import threading
import time
from importlib import resources
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import anyio  # ships with `mcp`; used to offload blocking tools off-loop

from . import (
    __version__,
    _debug_log,
    _stdio_transport,
    config,
    serve_metrics,
    service_config,
)
from .catalog import render_efforts
from .corpus import describe, kind_status
from .corpus_store import get_corpus_store
from .digest.overview import (
    _label_frequencies,
    _subject_prefix_frequencies,
    build_overview,
)
from .digest.query import parse_md_tables, query_digest
from .embeddings import (
    _get_embed_model,
    any_indexed_wg,
    is_remote_embed_model,
    DEFAULT_EMBED_MODEL,
    chunk_counts,
    find_chunks_by_url,
    get_chunk,
    get_messages,
    index_model,
    probe_index,
    search,
)
from .freshness import freshness_line, last_gathered, staleness_warning
from .rfcs import render_rfc, render_search
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
    LogLevel,
    Verbosity,
    get_index_dir,
    graceful_keyboard_interrupt,
    log,
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
    return get_corpus_store().list_corpora()


def _files_dir(wg: str) -> str:
    """The local `files/` directory for `wg`'s current version, via the corpus
    store — which materialises a cloud version onto local scratch, or returns
    the live cache dir for the local backend. Every read tool is guarded by
    `_requires_corpus`, so the corpus is known to exist by the time this is
    called; a None here means it vanished mid-request and is a real error.

    The version is resolved per call. Pinning one version across all of a
    request's reads — so a concurrent publish cannot tear a multi-read tool —
    is a later refinement (a request-scoped version context) and affects only
    the cloud backend; the local backend is single-version.
    """
    cache = get_corpus_store().local_cache_dir(wg)
    if cache is None:
        raise FileNotFoundError(f"no current version for corpus {wg!r}")
    return cache


def _safe_path(wg: str, file: str) -> Optional[str]:
    """Resolve `file` inside the corpus's file cache; refuse path escapes."""
    cache = _files_dir(wg)
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
    cache = _files_dir(wg)
    path = digest_path(cache, kind)
    return path if os.path.isfile(path) else None


def _available_digest_kinds(wg: str) -> List[str]:
    """The digest kinds this corpus actually has on disk."""
    cache = _files_dir(wg)
    return [k for k in _DIGEST_KINDS if os.path.isfile(digest_path(cache, k))]


def _missing_digest_message(wg: str, kind: str) -> str:
    """Explain a missing digest by what the corpus *has*, not the
    universal kind list — so a valid-but-ungathered kind (e.g. `issues`
    for a corpus with no GitHub repos) reads as absent, not invalid."""
    if kind not in _DIGEST_KINDS:
        return (
            f"Unknown digest kind '{kind}'. "
            f"Valid kinds: {', '.join(_DIGEST_KINDS)}."
        )
    available = _available_digest_kinds(wg)
    if not available:
        return f"No digests for {wg} yet. Run `ietf-llm {wg}` to generate them."
    hint = ""
    if kind == "issues":
        hint = (
            " (Issues come from GitHub; none were gathered for this corpus — "
            "add repos with `ietf-llm "
            f"{wg} --github owner/repo`.)"
        )
    return (
        f"{wg} has no '{kind}' digest. "
        f"This corpus has: {', '.join(available)}.{hint}"
    )


def _corpus_exists(wg: str) -> bool:
    """True if `wg` has a cache directory. Read-only: unlike
    `get_wg_file_cache_dir`, it never creates one — so a typo'd corpus
    name is not silently materialised by a query."""
    return get_corpus_store().corpus_exists(wg)


def _requires_corpus(fn: Callable[..., str]) -> Callable[..., str]:
    """Guard a `tool_*(wg, ...)` so an unknown corpus returns a clear
    message — rather than creating a junk cache dir and rendering a hollow
    result from it."""

    @functools.wraps(fn)
    def wrapper(wg: str, *args: Any, **kwargs: Any) -> str:
        if not _corpus_exists(wg):
            return (
                f"Unknown corpus '{wg}'. Nothing is cached under that name — "
                f"run `ietf-llm {wg}` to gather it, or call `list_corpora` to "
                "see what is available."
            )
        return fn(wg, *args, **kwargs)

    return wrapper


def _invalid_date_message(value: Optional[str], field: str) -> Optional[str]:
    """Error message if `value` is set but not a real `YYYY-MM-DD` date,
    else None — so a fat-fingered date fails loudly instead of silently
    matching nothing."""
    if not value:
        return None
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return (
            f"Invalid {field} date {value!r} — use ISO format YYYY-MM-DD "
            "(e.g. 2026-05-01)."
        )
    return None


#: Upper bound on `k` for search_corpus, so a huge value can't return
#: thousands of chunks and blow the context window.
_MAX_SEARCH_K = 100


# --- Tool implementations (plain functions, also usable for unit tests) -----


def _with_freshness(wg: str, body: str) -> str:
    """Prepend the freshness line (gather date, escalating to a refresh
    warning when stale) to a tool response.

    Top-level tools call this; pivot tools (get_chunk_text,
    read_file_section) skip it because the line has already been seen on
    the call that surfaced the file in the first place.
    """
    line = freshness_line(wg)
    if not line:
        return body
    return f"{line}\n\n{body}"


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
    "`list_labels(wg)` for the corpus's curation vocabulary._"
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


@_requires_corpus
def tool_overview(wg: str) -> str:
    return _with_freshness(wg, build_overview(wg, _files_dir(wg)))


def tool_read_ietf_norms() -> str:
    """Return the bundled IETF.md — interpretive norms for reading
    a corpus (consensus, who-speaks-for-whom, list-vs-meeting).

    Factored out of the server instructions so the always-on context
    stays focused on tool routing; clients pull this on demand when
    the question is "what did the WG decide / who supports what."
    """
    try:
        path = resources.files("ietf_llm").joinpath("data/skill/IETF.md")
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return (
            "(IETF.md is missing from the installed package — "
            "try reinstalling: pipx install --force ietf-llm)"
        )


@_requires_corpus
def tool_list_labels(wg: str) -> str:
    """The corpus's curation vocabulary — GitHub issue labels AND mailing-
    list subject-prefix clusters — with their frequencies, sorted by
    count descending.

    Two sources because two WG-management styles exist: issue-driven
    groups (httpbis, aipref) tag with GitHub labels; mail-driven
    groups (TLS, with `[mlkem]` / `[ech]`) cluster on the list. The
    consumer doesn't have to know which the WG uses — both render.
    """
    cache = _files_dir(wg)
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
        example_prefix = prefixes[0][0]
        lines.append(
            f'_Use with `read_digest("{wg}", kind="threads", '
            f'subject="{example_prefix}")` to read every thread carrying '
            "the prefix, or with subject in `search_corpus` `file_pattern`."
            "_"
        )
        lines.append("")
    return _with_freshness(wg, "\n".join(lines))


@_requires_corpus
def tool_find_citations(wg: str, draft_name: str) -> str:
    """Return every thread / issue file that cites the given draft.

    A "citation" is one distinct source (thread or issue) that references
    the draft, de-duplicated per source — a thread mentioning it three
    times counts once. So the `cited in N` figure in `overview` is the
    cumulative count of such sources across the gathered corpus; it is not
    weighted by recency, so read it as accumulated attention, not
    necessarily current activity.

    Reads `digests/citations.md` (built at gather time by
    `gather.citations.scan_citations`). Draft name is normalised the
    same way the scanner normalises matches (lowercase, version
    suffix stripped), so `draft-Foo-Bar-07` and `draft-foo-bar` both
    yield the same result.
    """
    cache = _files_dir(wg)
    citations_md = digest_path(cache, "citations")
    if not os.path.isfile(citations_md):
        return _with_freshness(
            wg,
            f"No citations digest for {wg}. Either no thread / issue "
            "files reference any drafts, or this corpus was gathered with "
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


@_requires_corpus
def tool_list_files(wg: str, pattern: Optional[str] = None) -> str:
    cache = _files_dir(wg)
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

#: `(reply to [P])` marker inside a message header — captures the
#: per-file parent index P.
_REPLY_TO_RE = re.compile(r"\(reply to \[(\d+)\]\)")
#: A leading `[N] ` per-file index on a stored message title, stripped
#: when read_topic re-numbers globally.
_LEADING_BRACKET_RE = re.compile(r"^\s*\[\d+\]\s*")


def _strip_message_header(text: str) -> str:
    """Drop a stored message chunk's leading `### [N] …` section-header
    line, keeping the `_Subject:_` / `_Archived-At:_` lines and body — so
    a caller that renders its own header does not show two."""
    first, _, rest = text.partition("\n")
    if first.lstrip().startswith("### ["):
        return rest.lstrip("\n")
    return text


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
    the corpus cache (so include_replies degrades gracefully — we just skip
    the expansion rather than erroring the whole call)."""
    path = _safe_path(wg, file)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _thread_sizes(wg: str) -> Dict[str, Tuple[str, str]]:
    """`{file: (msgs, participants)}` from the threads digest, so the
    read_topic thread map can show real thread size, not just how many
    chunks matched the query. Empty when there's no threads digest."""
    path = _digest_path(wg, "threads")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            sections = parse_md_tables(fh.read())
    except OSError:
        return {}
    out: Dict[str, Tuple[str, str]] = {}
    for section in sections:
        cols = [c.lower() for c in section.columns]
        if "file" not in cols or "msgs" not in cols:
            continue
        i_file, i_msgs = cols.index("file"), cols.index("msgs")
        i_part = cols.index("participants") if "participants" in cols else None
        for row in section.rows:
            if i_file >= len(row):
                continue
            file = row[i_file].strip().strip("`")
            msgs = row[i_msgs] if i_msgs < len(row) else ""
            part = row[i_part] if i_part is not None and i_part < len(row) else ""
            out[file] = (msgs, part)
    return out


def _topic_thread_map(
    wg: str, matched_hits: List[Any], rows: List[Any], limit: int = 8
) -> List[str]:
    """A saturation signal for read_topic: how the matches spread across
    threads, with each thread's match count, how many were shown, and its
    real size. Lets a caller see which cluster is major and which thread
    it has only partly seen — so it knows when to read one in full rather
    than keep slicing. Empty for a single-thread topic (no map needed)."""
    matched_per_file: Dict[str, int] = {}
    for hit in matched_hits:
        matched_per_file[hit.file] = matched_per_file.get(hit.file, 0) + 1
    if len(matched_per_file) <= 1:
        return []
    shown_per_file: Dict[str, int] = {}
    for row in rows:
        if row[6]:  # is_matched
            shown_per_file[row[1]] = shown_per_file.get(row[1], 0) + 1
    sizes = _thread_sizes(wg)
    ranked = sorted(matched_per_file.items(), key=lambda kv: kv[1], reverse=True)
    out = [
        f"## Threads in this topic ({len(matched_per_file)})",
        "_How the matches spread across threads — a thread with many matches "
        "you have only partly seen is the one to read in full "
        "(`read_file_section`)._",
    ]
    for file, n_matched in ranked[:limit]:
        shown = shown_per_file.get(file, 0)
        size = sizes.get(file)
        size_tag = (
            f" · thread has {size[0]} msgs, {size[1]} participants" if size else ""
        )
        out.append(f"- `{file}` — {n_matched} matched, {shown} shown{size_tag}")
    if len(ranked) > limit:
        out.append(f"_… and {len(ranked) - limit} more thread(s)._")
    out.append("")
    return out


@_requires_corpus
def tool_read_topic(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    wg: str,
    query: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    file_pattern: Optional[str] = None,
    k: int = 20,
    include_replies: bool = False,
    body_chars: Optional[int] = None,
) -> str:
    # Per-message body cap: default 4000, but a synthesis task can dial it
    # down to spend less context. Clamp to [200, default] — lowering only.
    body_cap = _READ_TOPIC_MAX_BODY_CHARS
    if body_chars is not None and body_chars > 0:
        body_cap = max(100, min(body_chars, _READ_TOPIC_MAX_BODY_CHARS))
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
    thread_issue_hits = [
        h for h in hits if h.file.lower().startswith(("threads/", "issues/"))
    ]
    matched = thread_issue_hits[:k]
    # For the completeness signal: how many more matched than we show, and
    # whether the relevance shortlist itself was capped (so the true total
    # may be higher still). Per-match relevance scores let the caller spot
    # an off-topic match instead of silently discarding it.
    extra_matches = len(thread_issue_hits) - len(matched)
    fetch_capped = len(hits) >= fetch_k
    score_by_key = {(h.file, h.chunk_idx): float(h.score) for h in matched}
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
    # Completeness signal: read_topic is a relevance-ranked slice, not a
    # thread dump. Say so, say what was left out, and point at the paths to
    # the whole debate — the thing a "controversy" question most needs.
    out.append(
        "_This is a **relevance-ranked slice** (semantic match on the query, "
        "then date-ordered) — NOT a complete thread. Messages that do not "
        "match the query are not here; check each `rel=` score and discount "
        "low ones as possible off-topic noise._"
    )
    if extra_matches > 0 or fetch_capped:
        more = f"{extra_matches}+ more" if extra_matches > 0 else "more"
        out.append(
            f"_⚠ Not the whole debate: {more} message(s) matched beyond the "
            f"{len(matched)} shown — raise `k` (now {k}). For completeness: "
            "read a thread end-to-end with `read_file_section`, enumerate a "
            'topic\'s threads with `read_digest(kind="threads", '
            'subject="[…]")` or `find_citations`, and pass `file_pattern=` '
            "to cut cross-topic noise._"
        )
    out.append(
        "_Messages are numbered `[1..N]` in this chronological order; a "
        "`reply to [k]` points to that number here, not a per-file index. "
        "`chunk` is the per-file index for `get_chunk_text` / `find_replies`._"
    )
    if truncated_note:
        out.append(truncated_note)
    out.append("")

    # Thread map: how the matches spread across threads (a saturation
    # signal), so a consumer can see which cluster is major and which
    # thread they have only partly seen — before reading the messages.
    out.extend(_topic_thread_map(wg, thread_issue_hits, rows))

    # Global chronological numbers so a consumer can reference a message
    # unambiguously — the per-file `[N]` repeats across files in one
    # narrative ([1][2][1][2]…), and a stored "(reply to [P])" points at a
    # per-file index that means nothing across the merged timeline.
    seq_by_key = {(r[1], r[2]): i for i, r in enumerate(rows, 1)}
    for seq, row in enumerate(rows, 1):
        chunk_date, file, chunk_idx, title, text, url, is_matched = row
        tag = "matched" if is_matched else "reply"
        # Map the per-file "(reply to [P])" marker to the parent's global
        # number, when that parent is in view.
        parent_note = ""
        reply_match = _REPLY_TO_RE.search(text.split("\n", 1)[0])
        if reply_match:
            parent_seq = seq_by_key.get((file, int(reply_match.group(1))))
            if parent_seq:
                parent_note = f"  ·  reply to [{parent_seq}]"
        # Strip the title's own leading `[N]` and per-file `(reply to [P])`
        # — both are per-file indices the global numbering replaces.
        who = _LEADING_BRACKET_RE.sub("", title)
        who = _REPLY_TO_RE.sub("", who).strip()
        # Show the relevance score on matched messages so a weak (likely
        # off-topic) match is visible rather than blending into the arc.
        score = score_by_key.get((file, chunk_idx))
        rel = f"  ·  rel={score:.2f}" if is_matched and score is not None else ""
        out.append("---")
        out.append("")
        out.append(f"## [{seq}] {who}  ·  [{tag}]{rel}{parent_note}")
        meta_bits = [f"_file:_ `{file}`", f"_chunk:_ {chunk_idx}"]
        if url:
            meta_bits.append(f"_url:_ {url}")
        out.append("  ·  ".join(meta_bits))
        out.append("")
        body = _strip_message_header(text).strip()
        if len(body) > body_cap:
            body = body[: body_cap - 1] + "…"
            out.append(body)
            out.append("")
            out.append(
                f"_[message truncated at {body_cap} "
                f"chars; full body: `get_chunk_text({wg!r}, {file!r}, "
                f"{chunk_idx})`]_"
            )
        else:
            out.append(body)
        out.append("")

    return _with_freshness(wg, "\n".join(out))


@_requires_corpus
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
    a semantic search that scatters across the corpus. Walks the reply
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
    for _date, idx, _title, body, _url in kept:
        out.append("---")
        out.append("")
        # The stored chunk already opens with its own
        # `### [N] DATE — Sender (reply to [P])` header plus the
        # `_Subject:_` / `_Archived-At:_` lines, so render it as-is
        # rather than prepending a second, near-identical header. The
        # `[N]` in that header is the chunk_idx.
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


@_requires_corpus
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
    cache_dir = _files_dir(wg)
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


@_requires_corpus
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
    collapse_versions: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    # Clamp k to a sane range so a huge value can't return thousands of
    # chunks (context bomb) and a negative one can't behave oddly.
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    # Over-fetch when we will thin the results — `group_by="file"`
    # rolls up per file, and `collapse_versions` drops older draft revs —
    # so the final list still reaches `k` distinct items.
    over_fetch = group_by == "file" or collapse_versions
    fetch_k = max(k * 4, 20) if over_fetch else k
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
    dropped = 0
    if collapse_versions:
        hits, dropped = _collapse_draft_versions(hits)
    note = ""
    if dropped:
        note = (
            f"\n_{dropped} older draft revision(s) hidden — the latest "
            "matching revision is shown. Pass `collapse_versions=False`, or "
            "a versioned `file_pattern` (e.g. `drafts/%-04.txt`), for older "
            "revisions._"
        )
    # When the consumer is asking a breadth question ("which threads
    # discuss X?"), the default per-chunk hit list shows the same
    # thread five times — wasting context. group_by="file" collapses
    # to one row per file with hit count + best chunk; the per-chunk
    # view stays the default for depth questions.
    if group_by == "file":
        return _with_freshness(wg, _render_file_grouped(hits, k) + note)
    hits = hits[:k]
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
    return _with_freshness(wg, "\n".join(lines) + note)


#: Upper bound on how many corpora one `search_corpora` call will fan
#: across, so an over-long list can't turn a single call into a heavy
#: multi-index scan. Corpora past the cap are dropped with a note (never
#: silently truncated).
_MAX_SEARCH_CORPORA = 12


def _dedup_corpus_names(corpora: List[str]) -> List[str]:
    """Strip / drop blanks / de-dup the requested corpus names, preserving
    first-seen order. Non-string entries are ignored defensively."""
    seen: set[str] = set()
    out: List[str] = []
    for name in corpora:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def tool_search_corpora(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    corpora: List[str],
    query: str,
    k: int = 10,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    collapse_versions: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    requested = _dedup_corpus_names(corpora or [])
    if not requested:
        return (
            "search_corpora needs an explicit `corpora` list — the few "
            "efforts that dominate the topic, not a blind scan. Use "
            "`find_efforts(topic)` to discover candidates and `list_corpora` "
            "to see what is already cached, then pass the chosen names."
        )
    # Bound the fan-out. Excess corpora are reported, not silently dropped.
    dropped_for_cap: List[str] = []
    if len(requested) > _MAX_SEARCH_CORPORA:
        dropped_for_cap = requested[_MAX_SEARCH_CORPORA:]
        requested = requested[:_MAX_SEARCH_CORPORA]
    # Read-only existence check first, so a typo'd name is reported rather
    # than materialising a junk cache (see `_corpus_exists`).
    unknown = [c for c in requested if not _corpus_exists(c)]
    known = [c for c in requested if _corpus_exists(c)]
    # Clamp k to the same sane range as single-corpus search.
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    # Per corpus: read its embedding-model id (governs score comparability),
    # then run the same single-corpus search() and tag the hits.
    per_corpus: Dict[str, List[Any]] = {}
    model_by_corpus: Dict[str, str] = {}
    no_index: List[str] = []
    empty: List[str] = []
    dropped_versions = 0
    for corpus in known:
        model = index_model(corpus)
        if model is None:
            no_index.append(corpus)
            continue
        # Over-fetch when collapsing draft revisions so each corpus can
        # still contribute k distinct items to the merge.
        fetch_k = max(k * 4, 20) if collapse_versions else k
        hits = search(
            corpus,
            query,
            k=fetch_k,
            since=since,
            until=until,
            label=label,
            state=state,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
            verbose=Verbosity.QUIET,
        )
        if collapse_versions and hits:
            hits, dropped = _collapse_draft_versions(hits)
            dropped_versions += dropped
        if not hits:
            empty.append(corpus)
            continue
        per_corpus[corpus] = hits[:k]
        model_by_corpus[corpus] = model

    # Skip diagnostics — surfaced whether or not any hits came back, so a
    # caller always learns which requested corpora contributed nothing.
    skip_notes: List[str] = []
    if unknown:
        skip_notes.append(f"unknown (not gathered): {', '.join(unknown)}")
    if no_index:
        skip_notes.append(
            "no embedding index — run `ietf-llm <name>`: " + ", ".join(no_index)
        )
    if empty:
        skip_notes.append(f"no matching hits: {', '.join(empty)}")
    if dropped_for_cap:
        skip_notes.append(
            f"dropped over the {_MAX_SEARCH_CORPORA}-corpus cap: "
            + ", ".join(dropped_for_cap)
        )

    if not per_corpus:
        body = [f"(no results for {query!r} across the requested corpora)"]
        body += [f"_Skipped — {n}._" for n in skip_notes]
        return "\n".join(body)

    # Group corpora by embedding-model id (insertion order = first-seen).
    # Scores are directly comparable only within a group; see index_model.
    groups: Dict[str, List[str]] = {}
    for corpus in known:
        if corpus in per_corpus:
            groups.setdefault(model_by_corpus[corpus], []).append(corpus)

    # Rank within each group on raw score (comparable there).
    group_ranked: List[List[Tuple[str, Any]]] = []
    for members in groups.values():
        pool: List[Tuple[str, Any]] = [
            (corpus, hit) for corpus in members for hit in per_corpus[corpus]
        ]
        pool.sort(key=lambda ch: -ch[1].score)
        group_ranked.append(pool)

    if len(group_ranked) == 1:
        # One model across all corpora — a single comparable ranking.
        final = group_ranked[0][:k]
    else:
        # Differing models — interleave the per-group rankings round-robin
        # by rank position rather than merging on non-comparable scores.
        final = []
        idx = 0
        while len(final) < k and any(idx < len(g) for g in group_ranked):
            for grp in group_ranked:
                if idx < len(grp):
                    final.append(grp[idx])
                    if len(final) >= k:
                        break
            idx += 1

    queried = [c for c in known if c in per_corpus]
    lines: List[str] = []
    if len(groups) > 1:
        lines.append(
            "_Corpora use different embedding models, so scores are NOT "
            "comparable across them — grouped by model and interleaved by "
            "rank:_"
        )
        for model, members in groups.items():
            lines.append(f"_  • `{model}`: {', '.join(members)}_")
    else:
        only_model = next(iter(groups))
        lines.append(
            f"_Ranked across {len(queried)} corpora ({', '.join(queried)}); "
            f"all share embedding model `{only_model}`, so scores are "
            "directly comparable._"
        )
    # Surface stale corpora once, compactly — depth tools repeat the detail.
    stale = [c for c in queried if staleness_warning(c)]
    if stale:
        lines.append(f"_Stale (consider re-gathering): {', '.join(stale)}._")
    for note in skip_notes:
        lines.append(f"_Skipped — {note}._")
    lines.append("")

    for i, (corpus, hit) in enumerate(final, 1):
        loc = (
            f" lines={hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else ""
        )
        state_tag = f"  [{hit.state}]" if hit.state else ""
        lines.append(
            f"[{i}] corpus={corpus}  score={hit.score:.3f}  file={hit.file}  "
            f"chunk={hit.chunk_idx}{loc}{state_tag}"
        )
        lines.append(f"     {hit.title}")
        if hit.labels:
            lines.append(f"     labels: {hit.labels}")
        if hit.url:
            lines.append(f"     url: {hit.url}")
        lines.append(f"     {hit.snippet}")

    if dropped_versions:
        lines.append(
            f"\n_{dropped_versions} older draft revision(s) hidden across "
            "corpora; pass `collapse_versions=False` for older revisions._"
        )
    lines.append(
        "\n_Breadth view — this finds WHERE a topic lives across efforts. "
        "Pivot to the single-corpus tools for depth, using the `corpus=` "
        "tag: `search_corpus(corpus, query, ...)`, `read_topic`, "
        "`tally_positions`, `read_digest`; read a hit with "
        "`get_chunk_text(corpus, file, chunk)` / `read_file_section`._"
    )
    return "\n".join(lines)


#: A draft file with a 2-digit revision suffix, e.g.
#: `drafts/draft-ietf-httpbis-rfc6265bis-04.txt`. RFC files
#: (`drafts/rfc9110.txt`) have no revision and are never collapsed.
_DRAFT_REV_RE = re.compile(r"^(?P<stem>drafts/draft-.+)-(?P<rev>\d{2})\.txt$")


def _collapse_draft_versions(hits: List[Any]) -> "Tuple[List[Any], int]":
    """Drop a hit from an older draft revision when a newer revision of the
    same draft is also in the result set.

    Searching across every gathered draft revision otherwise returns the
    same section several times (`…-rfc6265bis-04`, `-02`, `-22`, …). Keep
    the newest revision that actually matched per draft stem; older
    revisions stay reachable via `collapse_versions=False` or a versioned
    `file_pattern`. Non-draft hits (RFCs, threads, issues, meetings) pass
    through untouched. Returns `(kept, dropped_count)`.
    """
    latest: Dict[str, int] = {}
    for hit in hits:
        match = _DRAFT_REV_RE.match(hit.file)
        if match:
            stem, rev = match.group("stem"), int(match.group("rev"))
            latest[stem] = max(latest.get(stem, -1), rev)
    kept: List[Any] = []
    dropped = 0
    for hit in hits:
        match = _DRAFT_REV_RE.match(hit.file)
        if match and int(match.group("rev")) != latest[match.group("stem")]:
            dropped += 1
            continue
        kept.append(hit)
    return kept, dropped


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


@_requires_corpus
def tool_fetch_by_url(wg: str, url: str) -> str:
    """Resolve a citation URL to its cached corpus content.

    Exact-match on the `url` column the chunker stamped at index time.
    Two cases by URL kind:

    - **Thread `Archived-At:` permalink** (`https://www.w3.org/mid/...`)
      → matches exactly one chunk (per-message). Returned as a single
      chunk.
    - **GitHub issue URL** → matches every chunk in the per-issue file
      (file-level URL). Returned as the file's concatenated content,
      since the consumer almost certainly wants the issue, not just
      its frontmatter header.
    """
    matches = find_chunks_by_url(wg, url)
    if not matches:
        return (
            f"No cached chunk for {url}. fetch_by_url resolves the URL forms "
            "stamped in the corpus: mailing-list permalinks "
            "(`https://www.w3.org/mid/<message-id>`, the `Archived-At:` link "
            "on each thread message) and GitHub issue URLs. A "
            "`mailarchive.ietf.org` URL will not match — use the message's "
            "`Archived-At:` link instead. If you expected a match, the index "
            f"may predate the `url` column (run `ietf-llm {wg} "
            "--rebuild-embeddings`)."
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


def _prewarm_one(model_name: str) -> None:
    """Construct the embedding model and, for on-device models, force the
    lazy weight load with a real embed.

    A remote OpenAI-compatible backend has no weights to warm; constructing
    the client is enough, and we must NOT make a network round-trip on the
    prewarm path (R10: readiness must not depend on an upstream call).
    """
    model = _get_embed_model(model_name, Verbosity.QUIET)
    if model is not None and not is_remote_embed_model(model_name):
        list(model.embed("warmup"))


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
    # Scan the index dir (defaults to the cache root) for a model to warm.
    root = get_index_dir()
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
            _prewarm_one(model_name)
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


def tool_get_session_log(limit: int, since_seconds: Optional[float]) -> str:
    """Render the tail of the per-process debug log as JSON.

    Sync helper for the `get_session_log` MCP tool; lives next to
    `_offload` because it's part of the same diagnostic facility.
    Temporary — removed when stall investigation closes."""
    events = _debug_log.read_tail(limit=limit, since_seconds=since_seconds)
    payload = {
        "path": _debug_log.current_path(),
        "enabled": _debug_log.is_enabled(),
        "event_count": len(events),
        "events": events,
    }
    return json.dumps(payload, indent=2, default=str)


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

    A server-side deadline (`IETF_LLM_TOOL_TIMEOUT` seconds, default 120,
    0 to disable) bounds a stuck call: rather than hang to the client's
    multi-minute ceiling, it returns a clear, retryable error. Generous
    enough not to trip a legitimate first-time embedding-model load or a
    large file read.
    """
    # run_sync loses the return type through functools.partial; the
    # tool_* functions all return str, so cast.
    #
    # Telemetry: every call gets a request id and emits offload_start /
    # thread_started / thread_returned-or-error / offload_end events to
    # the debug log so stall investigations have something to chew on.
    # See ietf_llm/_debug_log.py.
    req_id = _debug_log.next_id()
    t0 = time.monotonic()
    _debug_log.log_event(
        req_id,
        "offload_start",
        tool=getattr(fn, "__name__", "tool"),
        args_positional=len(args),
        args_keys=list(kwargs.keys()),
    )

    def _instrumented() -> str:
        _debug_log.log_event(
            req_id,
            "thread_started",
            queue_wait=round(time.monotonic() - t0, 6),
        )
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # pylint: disable=broad-except
            _debug_log.log_event(
                req_id,
                "thread_error",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            raise
        _debug_log.log_event(
            req_id,
            "thread_returned",
            result_bytes=len(result) if isinstance(result, str) else None,
        )
        return result

    partial = functools.partial(_instrumented)
    timeout = _tool_timeout_seconds()
    status = "unknown"
    try:
        if timeout <= 0:
            result = cast(
                str,
                await anyio.to_thread.run_sync(partial, abandon_on_cancel=True),
            )
            status = "ok"
            return result
        with anyio.move_on_after(timeout):
            result = cast(
                str,
                await anyio.to_thread.run_sync(partial, abandon_on_cancel=True),
            )
            status = "ok"
            return result
        # Fell through `with` without returning → the deadline cancelled.
        status = "timeout"
    except BaseException:  # pylint: disable=broad-except,try-except-raise
        status = "exception"
        raise
    finally:
        elapsed = time.monotonic() - t0
        _debug_log.log_event(
            req_id,
            "offload_end",
            status=status,
            elapsed=round(elapsed, 6),
        )
        # RED per tool for the /metrics scrape (issue #40). `status` is
        # "ok" on success; "timeout"/"exception" both count as errors.
        serve_metrics.record_tool(
            getattr(fn, "__name__", "tool"),
            elapsed,
            error=status != "ok",
        )
    # Reached only when the deadline cancelled the await above; the worker
    # thread is abandoned (it finishes and frees its slot on its own).
    name = getattr(fn, "__name__", "tool")
    log(
        f"{name} exceeded {timeout:.0f}s deadline; returning a timeout error.",
        Verbosity.STATUS,
        level=LogLevel.ERROR,
    )
    return (
        f"(Tool timed out after {int(timeout)}s. This is usually transient — "
        "retry. If it persists: the embedding model may still be loading "
        "(first `search_corpus` after startup), or a concurrent `ietf-llm` "
        "gather may be holding the cache. Override with IETF_LLM_TOOL_TIMEOUT.)"
    )


def _tool_timeout_seconds() -> float:
    """Per-call deadline for `_offload`, from `IETF_LLM_TOOL_TIMEOUT`
    (seconds; default 120; non-positive disables)."""
    try:
        return float(os.environ.get("IETF_LLM_TOOL_TIMEOUT", "120"))
    except ValueError:
        return 120.0


def _gather_enabled() -> bool:
    """True when the operator has opted into the gather tools by setting
    `IETF_LLM_ENABLE_GATHER` truthy.

    Off by default: gather writes to the cache and reaches the network,
    which the rest of the server never does. Leaving it off preserves the
    read-only / no-network guarantee for the shared HTTP deployment; local
    users who want in-session gathering turn it on.
    """
    raw = os.environ.get("IETF_LLM_ENABLE_GATHER", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def tool_start_gather(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    corpus: str,
    mailing_list: Optional[List[str]] = None,
    draft: Optional[List[str]] = None,
    github: Optional[List[str]] = None,
    author: Optional[str] = None,
    new_drafts: bool = False,
    months: Optional[int] = None,
    add_mentioned_drafts: bool = False,
    include_related_drafts: bool = False,
    github_label: Optional[List[str]] = None,
    exclude_github_label: Optional[List[str]] = None,
    force: bool = False,
) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    corpus = (corpus or "").strip()
    if not corpus:
        return "Provide a corpus name to gather (e.g. a WG shortname like `tls`)."
    if not gather_runner.valid_corpus_name(corpus):
        return (
            f"'{corpus}' is not a valid corpus name. Use letters, digits, "
            "'.', '-' or '_' (no path separators or spaces), starting with a "
            "letter or digit."
        )
    spec = gather_runner.GatherSpec(
        corpus=corpus,
        mailing_list=list(mailing_list or []),
        draft=list(draft or []),
        github=list(github or []),
        github_label=list(github_label or []),
        exclude_github_label=list(exclude_github_label or []),
        author=author,
        new_drafts=new_drafts,
        months=months,
        add_mentioned_drafts=add_mentioned_drafts,
        include_related_drafts=include_related_drafts,
        force=force,
    )
    result = gather_runner.start(spec)
    if not result.get("started"):
        reason = result.get("reason")
        if reason == "similar exists":
            detail = result.get("detail", f"'{corpus}' overlaps an existing corpus.")
            return (
                f"{detail} Prefer querying the existing corpus over gathering a "
                f"near-duplicate. To mint '{corpus}' anyway, call "
                f'`start_gather(corpus="{corpus}", force=True)`.'
            )
        if reason == "fresh":
            detail = result.get("detail", f"'{corpus}' was gathered recently.")
            return (
                f"{detail} This is success, not an error — query '{corpus}' "
                "directly. To re-gather anyway, call "
                f'`start_gather(corpus="{corpus}", force=True)`.'
            )
        return (
            f"A gather for '{corpus}' is already running. "
            f'Poll `gather_status(corpus="{corpus}")` for progress.'
        )
    return (
        f"Started gathering '{corpus}' in the background (this can take "
        f'minutes). Poll `gather_status(corpus="{corpus}")` for stage-level '
        "progress; the corpus is queryable once it reports `done`."
    )


def tool_gather_status(corpus: Optional[str] = None) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    if corpus:
        corpus = corpus.strip()
        if not gather_runner.valid_corpus_name(corpus):
            return f"'{corpus}' is not a valid corpus name."
        status = gather_runner.read_status(corpus)
        if status is None:
            return (
                f"No gather has been recorded for '{corpus}'. Start one with "
                f'`start_gather(corpus="{corpus}")`.'
            )
        return _format_gather_status(status)
    statuses = gather_runner.all_statuses()
    if not statuses:
        return "No gathers have been recorded yet."
    return "\n".join(_format_gather_status(s) for s in statuses)


def _format_gather_status(status: Dict[str, Any]) -> str:
    """One compact line for a gather status record."""
    corpus = status.get("corpus", "?")
    state = status.get("state", "?")
    parts = [f"**{corpus}** — {state}"]
    if state == "running":
        idx = status.get("stage_index") or 0
        total = status.get("stage_total")
        stage = status.get("stage")
        if total:
            label = f"stage {idx}/{total}"
            if stage:
                label += f" ({stage})"
            parts.append(label)
        elif stage:
            parts.append(f"stage: {stage}")
    # An interrupted gather never finished, so its start->now span isn't a
    # meaningful "elapsed" (it would grow on every poll); omit it.
    if state != "interrupted":
        elapsed = _gather_elapsed(status)
        if elapsed:
            parts.append(elapsed)
    if state == "interrupted":
        parts.append("the gather process ended before completion; re-run it")
    if state == "failed" and status.get("error"):
        parts.append(f"error: {status['error']}")
    return " · ".join(parts)


def _parse_iso(value: Any) -> "Optional[datetime.datetime]":
    """Parse a trailing-Z ISO 8601 timestamp, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _gather_elapsed(status: Dict[str, Any]) -> str:
    """`45s` / `3m12s` between start and finish (or now, if running)."""
    started = _parse_iso(status.get("started"))
    if started is None:
        return ""
    end = _parse_iso(status.get("finished")) or datetime.datetime.now(
        datetime.timezone.utc
    )
    secs = int((end - started).total_seconds())
    if secs < 0:
        return ""
    if secs < 120:
        return f"{secs}s"
    return f"{secs // 60}m{secs % 60:02d}s"


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

    # Diagnostic facility for investigating client-side stalls/timeouts.
    # Off by default; opt in per session by setting IETF_LLM_DEBUG_LOG=1
    # in the MCP server's launch env. When on, writes JSONL per-request
    # timing to a per-pid file under ~/.cache/ietf-llm/_debug/, and the
    # `get_session_log` tool returns its tail to the client.
    _debug_log.init()

    # `instructions` is the MCP-spec mechanism for server-level
    # guidance: clients SHOULD surface it as system-prompt context.
    # Loading SKILL.md here makes the same guidance Claude Code reads
    # from the installed skill available to Codex / Gemini / Cursor /
    # Zed / opencode — one source of truth, no parallel maintenance.
    server_instructions = _load_server_instructions()
    server = FastMCP("ietf-llm", instructions=server_instructions)

    @server.tool()
    async def list_corpora() -> str:
        """List the IETF/IRTF efforts gathered locally by ietf-llm —
        working groups, research groups, mailing lists, and draft sets —
        each tagged with its **kind** and **status**. **Call this first**
        (a cheap orientation step) before answering any question about
        IETF/IRTF work, and whenever you don't know which `corpus` the
        user means — it is how you discover that the purpose-built corpus
        tools apply instead of falling back to web search.

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
    async def find_efforts(query: str, limit: int = 15) -> str:
        """Find active IETF/IRTF efforts by **topic** — the entry point
        for "what is the IETF doing around X?" when no working group is
        named. Returns a ranked markdown list of working/research groups,
        each tagged with whether it is **already gathered here** (`✓
        cached`); prefer those.

        This is the topic→effort discovery step the corpus-first tools
        lack. Reach here when the user gives a *subject* with no obvious
        home — "AI", "post-quantum", "congestion control", "email
        security" — instead of guessing a corpus or crawling Datatracker /
        the web. It ranks over the official Datatracker group list
        (acronym + name + charter description), mirrored locally; v1
        covers **active** groups only, so a concluded effort or published
        work won't surface here — use `rfc_search` for the RFC series, and
        `list_corpora` to see what is already cached.

        The playbook: `find_efforts(topic)` → present the candidates
        (prefer the cached ones) → gather the **few** efforts that
        dominate the topic (`start_gather` / `ietf-llm <acronym>`), not
        all of them, and tell the user what you skipped → query each
        gathered corpus → synthesize. On a shared server a wide gather
        fan-out costs everyone, so over-gathering is the failure mode to
        avoid.

        `limit` caps results (default 15).
        """
        return await _offload(render_efforts, query, limit)

    @server.tool()
    async def rfc_search(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        query: str,
        status: Optional[str] = None,
        stream: Optional[str] = None,
        level: Optional[str] = None,
        wg: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """Search the **published RFC series** by words in titles and
        keywords, returning a compact markdown list. A bare RFC number
        (e.g. "9110") short-circuits to that single RFC.

        This is the whole-series index (every RFC, all streams), mirrored
        from rfc.fyi — distinct from `search_corpus`, which is semantic
        search *within one gathered Working Group*. Reach here for "find
        an RFC about X", "which RFC is X", "what's the status of RFC N";
        reach for `search_corpus` for a corpus's own discussion of a topic.

        Optional filters narrow the result set:
          - `status`: `current` | `obsoleted`
          - `stream`: `ietf` | `irtf` | `iab` | `independent` |
            `editorial` | `legacy`
          - `level`: `std` | `bcp` | `informational` | `experimental` |
            `historic` | `unknown`
          - `wg`: an IETF working group acronym.
          - `limit`: max results (default 50).

        Follow a hit with `get_rfc(number)` for full metadata and its
        reference graph.
        """
        return await _offload(render_search, query, status, stream, level, wg, limit)

    @server.tool()
    async def get_rfc(number: str) -> str:
        """Full metadata for one RFC from the published series: title,
        status, stream, level, working group, keywords, what it
        obsoletes / is obsoleted by, its normative + informative
        references, how many RFCs cite it, and links to the text.

        `number` is an RFC number or name ("9110" or "RFC9110"). This is
        catalogue metadata, not the document body — to read the prose,
        follow the text link in the output.
        """
        return await _offload(render_rfc, number)

    @server.tool()
    async def overview(corpus: str) -> str:
        """Orient on an IETF/IRTF effort — a working group, research
        group, BoF, mailing list, or draft set — in one call: chairs/ADs,
        active drafts, top open issues, recent mailing list threads,
        latest meeting and latest draft publication.

        **Call this first** (alongside `list_corpora`) to orient before
        answering — and **prefer it to web search** — for ORIENTING /
        STRUCTURAL questions about an IETF WG, IRTF RG, or other corpus by
        shortname (`httpbis`, `quic`, `tls`, `aipref`, `cfrg`, `hrpc`, …):
        "what's happening in X?", "tell me about X", "what's X up to?",
        "who's on X?", "what is X working on?". The corpus is the gathered
        primary record; web search only sees second-hand coverage. ~30
        lines of markdown instead of the 80-100 KB of context that reading
        every digest would burn.

        **Skip overview and go straight to the specialised tool for
        TOPICAL questions:**
          - "arguments for/against X" / "scope debate about X" →
            `search_corpus(corpus, "X", label="...")` — issue labels are
            the corpus's own curation.
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
        `list_files`, `list_labels`. Interpretive norms (how
        consensus works, who-speaks-for-whom, list vs meeting):
        `read_ietf_norms`.
        """
        return await _offload(tool_overview, corpus)

    @server.tool()
    async def read_ietf_norms() -> str:
        """Return the interpretive norms for reading an IETF corpus:
        how consensus works (chair-declared, not vote-counted), how
        to attribute positions (individuals, not employers), and
        why mailing-list confirmation — not meeting agreement —
        is the binding decision.

        **Call this before** characterising what a WG decided, who
        supports what, or where the group stands. Not needed for
        catalogue lookups (`read_digest`), text fetches
        (`read_file_section`), or structural questions (`overview`).
        The content is stable across corpora — one call per session
        is enough.
        """
        return await _offload(tool_read_ietf_norms)

    @server.tool()
    async def list_labels(corpus: str) -> str:
        """List the corpus's curation vocabulary — GitHub issue labels
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
        """Find every mailing-list thread or GitHub issue in an IETF/IRTF
        effort that cites a given Internet-Draft.

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
        long-running corpus can have 1000+ files.

        `(digest)` rows are the per-corpus summary digests — read them via
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
        sort: Optional[str] = None,
        exclude_mechanical: bool = False,
    ) -> str:
        """Read filtered catalogue digests of an IETF/IRTF effort — its
        GitHub issues, mailing-list threads, participants (people),
        timeline of events, and file index. **Use this INSTEAD OF web
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
                            "doc-wglc"), limit.
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
        collapse_versions: bool = True,
    ) -> str:
        """Search the gathered record of an IETF/IRTF effort — a working
        group, research group, mailing list, or set of Internet-Drafts —
        semantically across its mailing-list debate, GitHub issues,
        drafts, RFCs, slides, transcripts, and minutes. Returns top-k
        chunks with file, chunk_idx, title, score, snippet, line range,
        GitHub URL (for issue chunks), and (for issue chunks) the issue's
        GitHub labels + open/closed state.

        **Use this INSTEAD OF web search** for any question about what an
        IETF/IRTF group discussed, debated, or decided about a topic —
        this reads the group's *actual* list traffic and issues, not the
        web's second-hand summary of them. Substantive "what was said
        about X?" / "what's the group's stance on Y?" questions land here.
        Pivot with `get_chunk_text` or `read_file_section` to read a hit
        in context.

        For topical questions ("arguments for/against X", "scope debate")
        try `label=` first — the corpus's own labels (e.g. "vocabulary",
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

        `collapse_versions=True` (the default) hides older draft
        revisions when a newer one of the same draft also matched, so a
        query does not return the same section as `…-rfc6265bis-04`,
        `-02`, `-22`. Set it False, or pin a revision with `file_pattern`
        (e.g. `"drafts/%-04.txt"`), to search a specific older revision.

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
            collapse_versions=collapse_versions,
        )

    @server.tool()
    async def search_corpora(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpora: List[str],
        query: str,
        k: int = 10,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        author: Optional[str] = None,
        role: Optional[str] = None,
        snippet_chars: Optional[int] = None,
        collapse_versions: bool = True,
    ) -> str:
        """Semantic search across **several** gathered corpora in one call,
        returning merged, rank-ordered hits each tagged with the `corpus=`
        it came from. The cross-corpus companion to `search_corpus` — same
        per-corpus engine, fanned over the set you name.

        **This is the synthesis step for a cross-cutting topic** ("what is
        the IETF doing around AI?", "where is post-quantum being worked
        on?"). The flow: `find_efforts(topic)` → gather the **few** efforts
        that dominate it → `search_corpora` across them in ONE call to see
        where the topic lives → then pivot to the single-corpus tools
        (`read_topic`, `tally_positions`, `read_digest`, `search_corpus`)
        for depth on the efforts that matter. Frame it as **breadth, not
        depth**: it finds *where* across efforts a topic appears;
        decisions, narrative, and consensus still come from the
        single-corpus tools and `read_ietf_norms`.

        `corpora` is a **required, explicit list** — the bounded set you
        chose (typically the cached output of `find_efforts`), never an
        unbounded scan of every cache. Unknown names, corpora with no
        embedding index, and any past the 12-corpus cap are skipped
        and reported, not silently dropped.

        **Score comparability.** Cosine scores are only directly
        comparable across corpora built with the **same embedding model**.
        When every corpus shares one model, the result is a single
        score-ranked list. When models differ, corpora are grouped by
        model, ranked within each group, and the groups are **interleaved
        by rank** rather than merged on raw score — and the header tells
        you which corpora were grouped together.

        `k` bounds the **total** merged hits returned (default 10). The
        facets mirror `search_corpus` and apply per corpus: `since` /
        `until` (ISO `YYYY-MM-DD`), `label`, `state` (`open` / `closed`),
        `author`, `role`, `snippet_chars`, `collapse_versions`. The
        depth-only knobs (`sort`, `group_by`, `file_pattern`) are
        intentionally omitted — scope a single corpus for those.

        Read-only; operates on existing caches, no re-gather needed.
        Requires each corpus's embedding index (built by default on
        gather).
        """
        return await _offload(
            tool_search_corpora,
            corpora,
            query,
            k=k,
            since=since,
            until=until,
            label=label,
            state=state,
            author=author,
            role=role,
            snippet_chars=snippet_chars,
            collapse_versions=collapse_versions,
        )

    @server.tool()
    async def find_replies(
        corpus: str,
        file: str,
        chunk_idx: int,
        max_messages: int = 20,
    ) -> str:
        """Return every transitive reply to a specific mailing-list
        thread message in an ietf-llm corpus, in chronological order,
        with full bodies.

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
        `LGTM`, conditional support, `DISCUSS`) per message author in ONE
        mailing-list thread or GitHub issue of an IETF/IRTF effort — the
        grounded way to read a group's **consensus / level of support**
        from the actual list traffic rather than relaying a summary or a
        web search. Output also
        includes a **Chair
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

        Pass `file` as a relative path under the corpus cache, e.g.
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
        body_chars: Optional[int] = None,
    ) -> str:
        """Read an IETF/IRTF effort's debate as a chronological narrative
        across its mailing list threads and GitHub issues. Returns the
        full text of every matched message — author, date, role,
        archived-at URL, body — in date order, oldest first.

        **Use this INSTEAD OF web search** when the user wants the *arc*
        of how a working group / research group discussion on a topic
        evolved — it reconstructs the real conversation from the gathered
        list and issue traffic, not the web's recap.

        `body_chars` caps each message body (default 4000; min 100). Dial
        it down for a synthesis task where the gist of each message is
        enough — the slice costs far less context, and a truncated body
        still points at `get_chunk_text` for the full text.

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

        IMPORTANT — this is a **relevance-ranked slice, not a complete
        thread**: messages are the top-`k` semantic matches for the query,
        then date-ordered. Messages that don't match the query are not
        included, and a low-scoring match may be off-topic. Each matched
        message carries a `rel=` score (higher = closer) so you can
        discount weak ones, and the header reports when more matched than
        were shown. When the matches span more than one thread, the output
        opens with a **thread map** — each thread the matches touch, how
        many matched vs. were shown, and the thread's real size — so you
        can spot a major cluster the slice barely sampled (read that one in
        full) instead of assuming the slice covered the debate. For a
        question about *completeness* (e.g. "the whole controversy"), do
        not treat the slice as exhaustive: raise `k`, scope with
        `file_pattern=` to cut cross-topic noise, read a thread end-to-end
        with `read_file_section`, or enumerate a topic's threads with
        `read_digest(kind="threads", subject="[…]")`.

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
            body_chars=body_chars,
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

        Note: per-corpus digests (`digests/*.md`) are not chunked — use
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
        """Resolve a citation URL to its cached chunk in a corpus.
        Accepts the URL forms that actually appear in the corpus:

        - Mailing-list message permalinks of the form
          `https://www.w3.org/mid/<message-id>` — this is the
          `Archived-At:` URL shown on every thread message (NOT
          `mailarchive.ietf.org/arch/msg/...`, which is not what the
          archive stamps into the messages).
        - GitHub issue URLs (e.g.
          `https://github.com/<owner>/<repo>/issues/<N>`).

        Matches exactly against the URL stamped at index time, so pass
        the URL as it appears in the data (a `w3.org/mid` link straight
        from a message header resolves; a hand-built archive URL will
        not). Returns the chunk text — same shape as `get_chunk_text`.
        Use it when the user pastes, or a chunk cites, such a URL.
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
        return await _offload(
            tool_read_file_section, corpus, file, start_line, max_lines
        )

    # `get_session_log` is only registered when telemetry is enabled
    # (IETF_LLM_DEBUG_LOG=1). With logging off the tool wouldn't have
    # anything useful to return, so we leave it out of the advertised
    # tool list entirely rather than ship a no-op tool.
    if _debug_log.is_enabled():

        @server.tool()
        async def get_session_log(
            limit: int = 200, since_seconds: Optional[float] = None
        ) -> str:
            """Return recent per-request telemetry from THIS MCP server
            process — a diagnostic facility for investigating
            client-side stalls/timeouts. Only registered when the
            server was launched with `IETF_LLM_DEBUG_LOG=1`.

            Each tool call emits a sequence of events keyed by request
            id: `offload_start` (dispatched off the event loop),
            `thread_started` (anyio worker picked it up — gap from
            `offload_start` is the thread-pool queue wait),
            `thread_returned` or `thread_error`, and `offload_end`
            (always emitted, with `status` ∈ ok / timeout / exception
            / unknown). A daemon thread also writes a `heartbeat` event
            every 10s so an idle process is distinguishable from a
            wedged one.

            Returns a JSON object with `path` (the full log file on
            disk), `enabled`, `event_count`, and `events`. If the
            client is reporting a stall, call this with `since_seconds`
            covering the stall window to get just the relevant tail.

            Args:
                limit: Max events to return from the tail (default 200,
                    use 0 for no limit).
                since_seconds: If set, only return events from the last
                    N seconds of process time.
            """
            return await _offload(tool_get_session_log, limit, since_seconds)

    # `start_gather` / `gather_status` write to the cache and reach the
    # network — the one break from this server's read-only / no-network
    # contract — so they are registered only when the operator opts in with
    # IETF_LLM_ENABLE_GATHER=1. Default off keeps the shared HTTP replica
    # read-only; local stdio users turn it on for in-session gathering.
    if _gather_enabled():

        @server.tool()
        async def start_gather(  # pylint: disable=too-many-arguments,too-many-positional-arguments
            corpus: str,
            mailing_list: Optional[List[str]] = None,
            draft: Optional[List[str]] = None,
            github: Optional[List[str]] = None,
            author: Optional[str] = None,
            new_drafts: bool = False,
            months: Optional[int] = None,
            add_mentioned_drafts: bool = False,
            include_related_drafts: bool = False,
            github_label: Optional[List[str]] = None,
            exclude_github_label: Optional[List[str]] = None,
            force: bool = False,
        ) -> str:
            """Gather a new corpus into the local cache, in the background.

            Use this when a corpus the user asks about isn't cached yet
            (`list_corpora` doesn't show it). Returns immediately; the
            gather runs for minutes. Poll `gather_status(corpus=...)` until
            it reports `done`, then the normal read tools work on it.

            The corpus **shape is inferred** from what you pass — you don't
            declare it:
            - **Working Group / RG / BoF**: pass just `corpus` as the
              shortname (`tls`, `cfrg`). The charter, drafts, RFCs,
              meetings, mailing list, and any GitHub issues are
              auto-discovered.
            - **Standalone mailing list**: pass `corpus` as the list name
              (`last-call`); auto-detected when it isn't a known group.
            - **Custom set**: any label as `corpus` plus explicit
              `mailing_list` / `draft` / `github` sources.
            - **Follow an author / new drafts**: `author` (email, person
              id, or exact name) or `new_drafts=True` (rolling window).
            - **Synthetic**: an `x-` `corpus` name with explicit sources.

            One gather per corpus runs at a time (a second call while one
            is in flight reports "already running"); different corpora run
            in parallel.

            A corpus gathered within the freshness window (default 6h) is
            **not** re-gathered — the call returns a "fresh, skipped" note.
            That is success: query the existing snapshot, don't retry. Only
            pass `force=True` when the user explicitly wants fresh data.

            Custom / synthetic (`x-`) names are free-form and don't self-
            deduplicate, so before minting one this checks `list_corpora` for
            an existing corpus over the same sources. On overlap it returns a
            reuse hint instead of gathering — prefer the existing corpus;
            `force=True` mints the near-duplicate anyway.

            Args:
                corpus: Corpus name — a WG/RG/BoF shortname, a mailing-list
                    name, or any label for a custom/synthetic corpus.
                mailing_list: Extra mailing lists to sync (bare name or
                    full address; domain optional).
                draft: Internet-Drafts to track (`draft-foo-bar`; version
                    suffix ignored, all revisions gathered).
                github: GitHub repos whose issues to gather (`owner/repo`).
                author: Make this a follow-an-author corpus (drafts by this
                    person; email is the unambiguous form).
                new_drafts: Make this a rolling 'new Internet-Drafts'
                    subscription over the `months` window.
                months: Months of mailing-list / meeting history to fetch.
                add_mentioned_drafts: Also pull in drafts the corpus cites
                    but doesn't already have.
                include_related_drafts: Also gather related (un-adopted)
                    drafts the WG follows. Can be large.
                github_label: Include only issues with these labels.
                exclude_github_label: Exclude issues with these labels.
                force: Re-gather even if the corpus is within the freshness
                    window. Use only on an explicit request for fresh data.
            """
            return await _offload(
                tool_start_gather,
                corpus,
                mailing_list,
                draft,
                github,
                author,
                new_drafts,
                months,
                add_mentioned_drafts,
                include_related_drafts,
                github_label,
                exclude_github_label,
                force,
            )

        @server.tool()
        async def gather_status(corpus: Optional[str] = None) -> str:
            """Report the progress of background gathers started with
            `start_gather`.

            With `corpus`, returns that corpus's state: `running` (with the
            current stage, e.g. `stage 7/17 (github issues)`, and elapsed
            time), `done`, `failed` (with the error), or `interrupted` (the
            server process ended mid-gather — re-run `start_gather`). With no
            argument, lists every recorded gather, most-recently-active
            first. Poll this after `start_gather`; once a corpus reports
            `done`, the read tools (`overview`, `search_corpus`, …) work on
            it.

            Args:
                corpus: The corpus to report on. Omit to list all.
            """
            return await _offload(tool_gather_status, corpus)

    _prewarm_embedding_model_async()
    if _resolve_transport() == "http":
        # Shared-server deployment: standard MCP Streamable HTTP. The
        # threaded-writer transport below is stdio-specific.
        _run_http(server)
        return
    # Replace FastMCP.run() with our own stdio transport. The default
    # upstream transport writes outbound responses on the asyncio loop
    # via `await stdout.write(...)`, and a slow client backpressures
    # those writes through the kernel pipe buffer — stalling every
    # queued response invisibly. Our transport hands serialized bytes
    # to a daemon thread via a bounded in-process queue, so the loop
    # never awaits a kernel write. See ietf_llm/_stdio_transport.py.
    anyio.run(_run_with_threaded_writer, server)


async def _run_with_threaded_writer(server: Any) -> None:
    """Wire FastMCP's lowlevel server up to our threaded-writer stdio
    transport. Mirrors `FastMCP.run_stdio_async` (the function our
    transport replaces) line-for-line, swapping the transport."""
    async with _stdio_transport.stdio_server_threaded_writer() as (
        read_stream,
        write_stream,
    ):
        # `_mcp_server` is the lowlevel `mcp.server.Server` instance
        # FastMCP wraps. Private attribute, but stable in practice —
        # the upstream `run_stdio_async` uses the same name.
        await server._mcp_server.run(  # pylint: disable=protected-access
            read_stream,
            write_stream,
            server._mcp_server.create_initialization_options(),  # pylint: disable=protected-access
        )


def _resolve_transport() -> str:
    """Return the selected MCP transport: 'http' or 'stdio' (default).

    stdio stays the default for local use; the shared-server deployment
    sets IETF_LLM_MCP_TRANSPORT=http (or 'streamable-http').
    """
    transport = os.environ.get("IETF_LLM_MCP_TRANSPORT", "stdio").strip().lower()
    return "http" if transport in ("http", "streamable-http") else "stdio"


def _corpora_freshness() -> "dict[str, Any]":
    """Bounded freshness summary across all cached corpora (R18).

    Reads only the per-corpus `last-gathered` sentinels -- no upstream
    call -- so a replica can report how stale the data it serves is
    without touching the network. `count` is every cached corpus;
    `tracked` is the subset carrying a sentinel (caches predating
    freshness tracking, or populated out of band, have none, so they
    count but aren't tracked). `oldest` / `newest` bound the staleness
    window without a per-corpus row, keeping the payload small on a box
    serving many corpora -- the per-corpus breakdown is the /metrics
    scrape's job. Both are null when nothing is tracked.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    wgs = _list_wgs()
    tracked = [(wg, when) for wg in wgs if (when := last_gathered(wg)) is not None]

    def _entry(item: "tuple[str, datetime.datetime]") -> "dict[str, Any]":
        wg, when = item
        return {
            "corpus": wg,
            "last_gathered": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_seconds": max(0, int((now - when).total_seconds())),
        }

    summary: "dict[str, Any]" = {
        "count": len(wgs),
        "tracked": len(tracked),
        "oldest": None,
        "newest": None,
    }
    if tracked:
        summary["oldest"] = _entry(min(tracked, key=lambda it: it[1]))
        summary["newest"] = _entry(max(tracked, key=lambda it: it[1]))
    return summary


def _readiness() -> "tuple[bool, dict[str, Any]]":
    """Readiness for the container, computed WITHOUT any upstream call (R18).

    Ready when the index dir is mounted AND a real corpus index actually
    opens. Probing one index (not just stat-ing the dir) catches an index
    that is present but unservable -- e.g. a WAL-mode DB on a read-only
    mount without IETF_LLM_INDEX_IMMUTABLE, or a truncated file -- which a
    bare directory check would false-green. An empty server (no corpora
    gathered yet) is still ready: the dir is fine and there is nothing to
    open. The embedding endpoint is reported as configured-or-not but its
    reachability is deliberately NOT probed: a slow / unreachable upstream
    must not flap readiness, and R18 forbids gating liveness on an embed.
    """
    index_dir = get_index_dir()
    index_ok = os.path.isdir(index_dir) and os.access(index_dir, os.R_OK)
    probe = "skipped"
    if index_ok:
        wg = any_indexed_wg()
        if wg is None:
            probe = "no-corpora"
        else:
            probe = "ok" if probe_index(wg) else "failed"
    ready = index_ok and probe != "failed"
    return ready, {
        "version": __version__,
        "index_dir": index_dir,
        "index_dir_usable": index_ok,
        "index_probe": probe,
        "embed_endpoint_configured": bool(
            os.environ.get("IETF_LLM_EMBED_BASE_URL", "").strip()
        ),
        "corpora": _corpora_freshness(),
    }


async def _health_endpoint(_request: Any) -> Any:
    # pylint: disable=import-outside-toplevel
    from starlette.responses import JSONResponse

    ready, detail = _readiness()
    return JSONResponse(
        {"status": "ok" if ready else "unavailable", **detail},
        status_code=200 if ready else 503,
    )


def _corpus_ages() -> "List[Tuple[str, int]]":
    """Per-corpus `last-gathered` age in seconds, for the freshness gauge.

    Reads only the per-corpus sentinels (no upstream call; R18). Untracked
    corpora -- those without a sentinel -- are omitted, leaving the gauge
    to carry only ages it can actually report. This is the per-corpus
    breakdown /health deliberately summarises rather than enumerates.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ages: "List[Tuple[str, int]]" = []
    for wg in _list_wgs():
        when = last_gathered(wg)
        if when is not None:
            ages.append((wg, max(0, int((now - when).total_seconds()))))
    return ages


async def _metrics_endpoint(_request: Any) -> Any:
    # pylint: disable=import-outside-toplevel
    from starlette.responses import PlainTextResponse

    body = serve_metrics.render(_corpus_ages())
    # Prometheus text exposition format v0.0.4.
    return PlainTextResponse(
        body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


def _http_app(server: Any) -> Any:
    """The Streamable HTTP ASGI app with GET /health and /metrics added.

    Both sit beside the MCP endpoint (/mcp) on the same app, so they
    share the app lifespan -- no wrapper, no lifespan propagation gotcha.
    /health is the human-glance readiness view (R18); /metrics is the
    Prometheus scrape view (issue #40). Neither makes an upstream call.
    """
    app = server.streamable_http_app()
    app.add_route("/health", _health_endpoint, methods=["GET"])
    app.add_route("/metrics", _metrics_endpoint, methods=["GET"])
    return app


def _resolve_bind() -> "Tuple[str, int]":
    """Resolve the HTTP bind host:port from the environment (defaults
    127.0.0.1:8000). A non-integer port falls back to 8000."""
    host = os.environ.get("IETF_LLM_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("IETF_LLM_MCP_PORT", "8000"))
    except ValueError:
        port = 8000
    return host, port


def _effective_embed_model() -> str:
    """The embedding model the serve/gather paths would actually use.

    Mirrors `config.merge_global`'s precedence with no CLI in play and no
    persistence side effect: env > global-persisted > default. Read-only,
    so it is safe to call at boot to validate the embed config."""
    env = os.environ.get("IETF_LLM_EMBED_MODEL", "").strip()
    if env:
        return env
    persisted = config.load_global().get("embed_model")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip()
    return DEFAULT_EMBED_MODEL


def _effective_no_embed() -> bool:
    """Whether gather would skip embedding (env > global-persisted)."""
    env = os.environ.get("IETF_LLM_NO_EMBED", "").strip()
    if env:
        return env.lower() in ("1", "true", "yes", "on")
    return bool(config.load_global().get("no_embed", False))


def _index_immutable_enabled() -> bool:
    """Whether IETF_LLM_INDEX_IMMUTABLE is set (matches storage's predicate)."""
    return os.environ.get("IETF_LLM_INDEX_IMMUTABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


#: Prefix marking a local, torch-backed embedding model (mirrors
#: embeddings.models._ST_PREFIX). A gather that embeds with one of these
#: on a torch-free image crashes deep in the pipeline.
_LOCAL_EMBED_PREFIX = "sentence-transformers/"


def _torch_importable() -> bool:
    """True if `torch` can be imported, without importing it.

    `find_spec` only inspects the import system, so a torch-free serve
    image pays nothing and a present-but-heavy torch isn't loaded just to
    answer the question."""
    import importlib.util  # pylint: disable=import-outside-toplevel

    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _is_loopback_host(host: str) -> bool:
    """Heuristic: is `host` a loopback bind (not externally reachable)?

    String-based on the common cases rather than resolving DNS at boot
    (slow, and a resolver hiccup must not gate startup). `0.0.0.0` / `::`
    (bind-all) and any routable address or unrecognised hostname are
    treated as non-loopback -- the safe default is to assume reachable
    and warn."""
    lowered = host.strip().lower()
    return lowered in ("localhost", "::1") or lowered.startswith("127.")


def _serve_posture(host: str, port: int) -> "Dict[str, str]":
    """The always-logged boot posture: what this process is actually doing."""
    model = _effective_embed_model()
    backend = "remote" if is_remote_embed_model(model) else "local"
    return {
        "transport": "http",
        "bind": f"{host}:{port}",
        "gather": "on" if _gather_enabled() else "off",
        "embed_backend": backend,
        "embed_model": model,
        "no_embed": "yes" if _effective_no_embed() else "no",
        "index_dir": get_index_dir(),
        "index_immutable": "yes" if _index_immutable_enabled() else "no",
        "store_backend": service_config.store_backend(),
    }


def _serve_config_problems(host: str) -> "Tuple[List[str], List[str]]":
    """Cross-knob consistency check for the HTTP serve path (issue #46).

    Returns (hard_errors, warnings). Transport is not the thing to gate
    on: HTTP + in-session gather is a supported, trusted-box shape (#41).
    We refuse only configs that cannot work, and warn (not refuse) on
    exposure -- the operator owns that boundary.
    """
    errors: "List[str]" = []
    warnings: "List[str]" = []

    gather = _gather_enabled()
    model = _effective_embed_model()
    remote = is_remote_embed_model(model)

    # 1a. gather must write the index; immutable says the mount is read-only.
    if gather and _index_immutable_enabled():
        errors.append(
            "IETF_LLM_ENABLE_GATHER=1 and IETF_LLM_INDEX_IMMUTABLE=1 "
            "contradict: gather must write the index, but immutable marks "
            "the mount read-only. Unset one."
        )

    # 1b. gather-on-torch-free with a local (torch-backed) embed model would
    # crash deep in the embed step. Hard refuse (a gather-enabled server that
    # cannot gather is a misconfiguration worth surfacing now).
    if (
        gather
        and not _effective_no_embed()
        and model.startswith(_LOCAL_EMBED_PREFIX)
        and not _torch_importable()
    ):
        errors.append(
            f"IETF_LLM_ENABLE_GATHER=1 with a local embedding model "
            f"({model}) but torch is not importable: gather's embed step "
            f"would crash mid-pipeline. Set an 'openai-embed/<model>' id "
            f"(IETF_LLM_EMBED_MODEL) with IETF_LLM_EMBED_BASE_URL, install "
            f"the 'local-embeddings' extra, or set IETF_LLM_NO_EMBED=1."
        )

    # 1c. Remote embed model but no endpoint: search_corpus (the read path
    # everyone uses) would fail confusingly at request time. Independent of
    # gather.
    if remote and not os.environ.get("IETF_LLM_EMBED_BASE_URL", "").strip():
        errors.append(
            f"Embedding model {model} is remote but IETF_LLM_EMBED_BASE_URL "
            f"is not set: search_corpus would fail at request time. Set the "
            f"endpoint base URL (e.g. https://host/v1)."
        )

    # 2. Exposure without auth: warn loudly, never block. Binding wide
    # behind a proxy is the intended production shape (#41).
    if not _is_loopback_host(host):
        msg = (
            f"binding to {host} (non-loopback): the server has no "
            f"authentication or rate limiting and assumes a trust boundary "
            f"(proxy / firewall) in front (#41)."
        )
        if gather:
            msg += (
                " gather is enabled, so an unauthenticated caller could "
                "trigger cache writes and network egress."
            )
        warnings.append(msg)

    # 3. Cloud corpus store selected but under-configured: reads (and any
    # gather publish) would fail at request time. Validate the required knobs
    # are present, upfront.
    backend = service_config.store_backend()
    if backend == "cloud":
        missing = [
            env
            for env, value in (
                ("IETF_LLM_CONTROL_DB", service_config.control_db()),
                ("IETF_LLM_BLOB_DIR", service_config.blob_dir()),
                ("IETF_LLM_SCRATCH_DIR", service_config.scratch_dir()),
            )
            if not value
        ]
        if missing:
            errors.append(
                "IETF_LLM_STORE_BACKEND=cloud but the corpus store is "
                "under-configured: missing " + ", ".join(missing) + "."
            )
    elif backend != "local":
        errors.append(
            f"IETF_LLM_STORE_BACKEND={backend!r} is not recognised "
            "(expected 'local' or 'cloud')."
        )

    return errors, warnings


def _validate_serve_config(host: str, port: int) -> None:
    """Log the boot posture, surface warnings, and refuse on hard errors.

    Called before binding so a contradictory or under-provisioned config
    fails fast at boot rather than minutes into a gather or on the first
    search_corpus (project preference: upfront validation over
    wait-then-fail). Raises SystemExit(1) on any hard error.
    """
    posture = _serve_posture(host, port)
    log(
        "serve posture: " + " ".join(f"{k}={v}" for k, v in posture.items()),
        level=LogLevel.STATUS,
    )
    errors, warnings = _serve_config_problems(host)
    for warning in warnings:
        log(f"WARNING: {warning}", level=LogLevel.STATUS)
    if errors:
        for error in errors:
            log(f"Refusing to start: {error}", level=LogLevel.ERROR)
        raise SystemExit(1)


def _run_http(server: Any) -> None:
    """Serve the MCP server over Streamable HTTP (R8).

    Binds to IETF_LLM_MCP_HOST / IETF_LLM_MCP_PORT (defaults
    127.0.0.1:8000). FastMCP's streamable_http_app() is a standard
    MCP-spec Streamable HTTP ASGI app, so a fronting proxy can be
    near-transparent. uvicorn ships transitively with `mcp`. The custom
    threaded-writer transport is stdio-specific and does not apply here.
    """
    import uvicorn  # pylint: disable=import-outside-toplevel

    host, port = _resolve_bind()
    # Boot-time config validation + posture banner (issue #46): fail fast
    # on contradictory / under-provisioned configs, warn on exposure.
    _validate_serve_config(host, port)
    # Startup preamble: version + freshness floor, mirroring what /health
    # reports, so a rolling-deploy log shows which build a replica is on
    # and how stale its caches are at boot. Under IETF_LLM_LOG_FORMAT=json
    # this is a one-line structured record a collector can ingest.
    fresh = _corpora_freshness()
    oldest = fresh["oldest"]
    floor = (
        f"oldest {oldest['corpus']} {oldest['age_seconds'] // 86400}d"
        if oldest
        else "none tracked"
    )
    log(
        f"ietf-llm {__version__} serving HTTP on {host}:{port}; "
        f"{fresh['count']} corpora ({fresh['tracked']} tracked, {floor})",
        level=LogLevel.STATUS,
    )
    uvicorn.run(_http_app(server), host=host, port=port)


if __name__ == "__main__":
    main()
