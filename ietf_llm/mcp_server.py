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
    coverage,
    serve_metrics,
    service_config,
)
from .access import note_access
from .catalog import render_efforts
from .corpus import describe, kind_status, status_cell
from .corpus_store import VersionVanished, get_corpus_store, pin_corpus_version
from .digest.overview import (
    _label_frequencies,
    _subject_prefix_frequencies,
    build_overview,
)
from .digest.query import parse_md_tables, query_digest
from .embeddings import (
    DEFAULT_EMBED_MODEL,
    _get_embed_model,
    any_indexed_wg,
    chunk_counts,
    find_chunks_by_url,
    get_chunk,
    get_messages,
    index_model,
    is_remote_embed_model,
    probe_index,
    related,
    search,
)
from .freshness import (
    freshness_line,
    gather_enabled,
    gather_suggestion,
    last_gathered,
    set_gather_default,
    staleness_warning,
)
from .gather.citations import normalize_draft_name
from .gather.documents_manifest import load_documents_manifest
from .paths import (
    agenda_path,
    digest_kind_from_relpath,
    digest_path,
    drafts_dir,
    issue_path,
    issues_dir,
    meetings_dir,
    minutes_path,
    polls_dir,
    transcripts_dir,
)
from .positions import (
    extract_chair_statements,
    file_supports_tally,
    load_people_context,
    read_file_text,
    render_tally,
    tally_thread,
)
from .rfcs import render_rfc, render_search
from .routing import DEFAULT_MIN_SCORE, route
from .utils import (
    LogLevel,
    Verbosity,
    get_index_dir,
    graceful_keyboard_interrupt,
    log,
    months_request_caution,
    months_request_error,
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
    return serve_metrics.timed_store(
        "list_corpora", lambda: get_corpus_store().list_corpora()
    )


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
    cache = serve_metrics.timed_store(
        "local_cache_dir", lambda: get_corpus_store().local_cache_dir(wg)
    )
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
        return (
            f"No digests for {wg} yet — "
            f"{gather_suggestion(wg, purpose='to generate them')}."
        )
    hint = ""
    if kind == "issues":
        add_repos = (
            f'`start_gather(corpus="{wg}", github=["owner/repo"])`'
            if gather_enabled()
            else f"`ietf-llm {wg} --github owner/repo`"
        )
        hint = (
            " (Issues come from GitHub; none were gathered for this corpus — "
            f"add repos with {add_repos}.)"
        )
    return (
        f"{wg} has no '{kind}' digest. "
        f"This corpus has: {', '.join(available)}.{hint}"
    )


def _regather_call(wg: str) -> str:
    """The in-session re-gather call used in index / rebuild hints: `force`,
    since the corpus is already cached and a fresh gather rebuilds the search
    index in its final stages. Distinct from `gather_suggestion`'s shell form,
    which names the index-specific CLI flag the in-session path doesn't have."""
    return f'`start_gather(corpus="{wg}", force=True)`'


def _corpus_exists(wg: str) -> bool:
    """True if `wg` has a cache directory. Read-only: unlike
    `get_wg_file_cache_dir`, it never creates one — so a typo'd corpus
    name is not silently materialised by a query."""
    return serve_metrics.timed_store(
        "corpus_exists", lambda: get_corpus_store().corpus_exists(wg)
    )


def _requires_corpus(fn: Callable[..., str]) -> Callable[..., str]:
    """Guard a `tool_*(wg, ...)` so an unknown corpus returns a clear message —
    rather than creating a junk cache dir and rendering a hollow result from it.

    Also resolves the corpus's current version **once** and pins it for the
    whole tool call, so every read in the request (files and the search index)
    stays on one version even if a publish lands mid-call (G-1). No-op pin on
    the single-version local backend.

    If a concurrent re-gather reaps the pinned version mid-call (only possible on
    the cloud backend, and only when two publishes land during a single call — a
    correctness backstop, not a hot path), the read raises `VersionVanished`; we
    re-run the whole call once on a fresh pin of the now-current version. The pin
    lasts only one call, so this is just 're-resolve and retry'. If the retry
    also can't get a good version, surface the actionable message instead."""

    @functools.wraps(fn)
    def wrapper(wg: str, *args: Any, **kwargs: Any) -> str:
        version = serve_metrics.timed_store(
            "resolve_current", lambda: get_corpus_store().resolve_current(wg)
        )
        if version is None:
            return (
                f"Unknown corpus '{wg}'. Nothing is cached under that name — "
                f"{gather_suggestion(wg, purpose='to gather it')}, or call "
                "`list_corpora` to see what is available."
            )
        # Record the access now the corpus is known to exist: this guard fronts
        # every per-corpus read tool but not the cross-corpus discovery tools
        # (list_corpora / search_corpora / which_corpus), so listing never
        # counts as use. Coarsened and best-effort — see access.note_access.
        note_access(wg)
        try:
            with pin_corpus_version(wg, version):
                return fn(wg, *args, **kwargs)
        except VersionVanished as vanished:
            try:
                with pin_corpus_version(wg, vanished.new_version):
                    return fn(wg, *args, **kwargs)
            except VersionVanished:
                return (
                    f"Corpus '{wg}' was re-gathered while this request ran and "
                    "the version being read was retired before a new one settled. "
                    "Re-run the request."
                )

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


def _inflight_refresh_note(wg: str) -> Optional[str]:
    """One-line caveat when a gather for `wg` is running on this host *right
    now*, so the freshness stamp above reflects the **previous** snapshot, not
    the refresh in flight — the trap a reader hits when an in-place re-gather
    leaves the read path stamped with the prior (often same-day) gather time.

    Network-free: keys off `local_inflight`, the in-process job registry, so it
    adds no control-plane round-trip to the read path. On the cloud backend it
    is silent — the read path there serves the last *published* version, never a
    half-written one, so no caveat is warranted. Best-effort, and deliberately
    in-process-scoped: a gather running in a *separate* local process (a
    `ietf-llm <corpus>` CLI run sharing the same cache) leaves no signal this
    process can see — the CLI writes only the `last-gathered` sentinel, not a
    status record — so that case is unflagged. Closing it would need the CLI to
    publish an in-process marker; out of scope here.
    """
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    status = gather_runner.local_inflight(wg)
    if not status:
        return None
    idx = status.get("stage_index") or 0
    total = status.get("stage_total")
    stage = status.get("stage")
    # `stage_index` initialises to 0 before the first progress call, so only
    # show the numeric "N/total" once a stage has actually been entered;
    # otherwise fall back to the stage name (or a bare "in progress").
    if total and idx:
        where = f"stage {idx}/{total}" + (f": {stage}" if stage else "")
    elif stage:
        where = f"stage: {stage}"
    else:
        where = "in progress"
    return (
        f"⚠ A refresh is running now ({where}) — the snapshot above is the "
        f"*previous* gather, not this run, and won't include the new material "
        f'until it reports `done` (poll `gather_status(corpus="{wg}")`).'
    )


def _with_freshness(
    wg: str, body: str, *, sources: "coverage.Sources | None" = None
) -> str:
    """Prepend the freshness line (gather date, escalating to a refresh
    warning when stale) plus the coverage window floor to a tool response.

    The coverage line tells a client how far *back* the corpus reaches — the
    floor on its view — so it knows to re-gather deeper rather than treat
    absence of older activity as "it didn't happen". Best-effort: a failure
    resolving the files dir (e.g. a version vanished mid-request) just omits
    the coverage line, like a missing freshness sentinel.

    `sources` lets a caller that has already inventoried the corpus (overview)
    pass it in, so the window line reuses that scan instead of redoing it.

    Top-level tools call this; pivot tools (get_chunk_text,
    read_file_section) skip it because the line has already been seen on
    the call that surfaced the file in the first place.
    """
    try:
        window = coverage.window_line(wg, _files_dir(wg), sources=sources)
    except OSError:
        window = None
    head = "\n".join(
        part
        for part in (freshness_line(wg), _inflight_refresh_note(wg), window)
        if part
    )
    if not head:
        return body
    return f"{head}\n\n{body}"


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


def _corpus_sources(wg: str) -> str:
    """Compact source inventory for `wg` in `list_corpora`, read-only — resolves
    an already-materialised files dir (never forces a cloud download) and
    degrades to empty when the corpus isn't staged locally."""
    cache = get_corpus_store().materialised_cache_dir(wg)
    if cache is None:
        return ""
    return coverage.compact_sources_line(cache)


def tool_list_corpora() -> str:
    wgs = _list_wgs()
    if not wgs:
        return f"(no corpora gathered yet — {gather_suggestion('<name>')})"
    rows = []
    for wg in wgs:
        kind, status = kind_status(wg)
        tag = f"{kind} · {status_cell(kind, status)}"
        rows.append((wg, tag, describe(wg), _corpus_sources(wg)))
    name_w = max(len(w) for w, _, _, _ in rows)
    tag_w = max(len(t) for _, t, _, _ in rows)
    lines = []
    for wg, tag, subject, sources in rows:
        line = f"{wg.ljust(name_w)}  {tag.ljust(tag_w)}"
        if subject:
            line += f"  {subject}"
        if sources:
            line += f"  ({sources})"
        lines.append(line.rstrip())
    return (
        "Gathered corpora (name · kind [· status] · what it's about · "
        "(sources)). **kind** is `group` (a WG/RG/edwg/BoF — accepts every "
        "tool), `list` (a mailing list gathered on its own), `custom` "
        "(explicit drafts/repos or a followed author), or `synthetic` (an "
        "`x-` corpus). **status** is the group state (`active` / `concluded` "
        "/ `bof` / …) for a `group`; a `list`, `custom`, or `synthetic` "
        "corpus is **not a chartered IETF effort**, and its status says so "
        "explicitly — a corpus existing here implies nothing about IETF "
        "standing, and an `x-` bundle or a standalone list is not a Working "
        "Group. The text after that is the corpus's "
        "subject — the group name, the list followed, the tracked author. "
        "The trailing `(…)` is the source inventory — which of mailing "
        "`list`, GitHub `issues`, `drafts`, `RFCs`, `minutes` are present — "
        "so you can tell what each corpus actually holds. Call `overview` for "
        "the gather window and the exact repos.\n\n"
        + "\n".join(lines)
        + _NEXT_TOOLS_HINT
    )


def _overview_live_reconciliation(wg: str, live: bool) -> str:
    """The optional 'live draft reconciliation' section appended to overview.

    Empty on a read-only (gather-disabled) deployment — the live tools aren't
    available there, so neither is this. When gather is enabled but `live` is
    off, a one-line pointer to the live check (the default stays offline and
    fast). When `live` is on, it cross-checks the cache's active-draft list
    against Datatracker (`live_lookup.reconcile_active_drafts`) and reports any
    divergence, so a stale curated list can't silently mislead an agenda.
    """
    if not gather_enabled():
        return ""
    if not live:
        return (
            "\n\n_The active-draft list above is from the gather cache and can "
            "lag Datatracker. Call `overview(corpus, live=True)` to reconcile "
            "it live, or `draft_status(name)` to check one draft._"
        )

    from . import live_lookup  # pylint: disable=import-outside-toplevel
    from .digest.overview import (  # pylint: disable=import-outside-toplevel
        active_draft_names,
    )

    recon, fetched = live_lookup.reconcile_active_drafts(wg, active_draft_names(wg))
    lines = ["\n\n## Live draft reconciliation\n"]
    if not recon.advanced and not recon.revived:
        lines.append(
            f"The {recon.checked} active draft(s) above match Datatracker; no "
            "adopted draft is missing or has advanced past the WG."
        )
    else:
        if recon.advanced:
            lines.append(
                "**Listed active here but past the WG on Datatracker** "
                "(drop from a WG agenda):"
            )
            lines.extend(f"- `{name}` — {state}" for name, state in recon.advanced)
            lines.append("")
        if recon.revived:
            lines.append(
                "**Active adopted drafts on Datatracker missing from the list "
                "above** (a cached snapshot likely expired then revived — "
                "re-gather, and consider for the agenda):"
            )
            lines.extend(
                f"- `{name}` — expires {expires}" for name, expires in recon.revived
            )
            lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


@_requires_corpus
def tool_overview(wg: str, live: bool = False) -> str:
    files_dir = _files_dir(wg)
    body = build_overview(wg, files_dir)
    # One full scan (incl. verbatim repo names) reused by both the inventory
    # below and the window line in _with_freshness.
    src = coverage.detect_sources(files_dir)
    inventory = coverage.sources_line(files_dir, sources=src)
    if inventory:
        deeper = (
            f'`start_gather(corpus="{wg}", months=N)`'
            if gather_enabled()
            else f"`ietf-llm {wg} --months N`"
        )
        body += (
            "\n\n## Coverage\n\n"
            f"**Sources:** {inventory}.\n\n"
            "_GitHub issues and drafts are the full set, not limited by the "
            "gather window. For activity older than the window above, "
            f"re-gather deeper with {deeper} — don't read absence as proof it "
            "didn't happen._"
        )
    body += _overview_live_reconciliation(wg, live)
    return _with_freshness(wg, body, sources=src)


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block (`--- ... ---`). Skill files carry
    `name:` / `description:` metadata for the skill router; MCP clients want
    only the body. Tolerates absence — returns the text unchanged."""
    stripped = text.lstrip()
    if stripped.startswith("---"):
        end_marker = stripped.find("\n---", 3)
        if end_marker != -1:
            body_start = stripped.find("\n", end_marker + 4)
            if body_start != -1:
                return stripped[body_start + 1 :].lstrip()
    return text


def _read_bundled_skill_body(skill: str) -> str:
    """Return the body (frontmatter stripped) of a bundled skill's `SKILL.md`,
    or a reinstall hint if it's missing.

    One source of truth: the same `data/skills/<skill>/SKILL.md` files that
    `--install-skills` installs are what the MCP norms tools (and the server
    `instructions` field) serve — so the guidance can't drift between the
    skill a Claude/Codex/Gemini/opencode user sees and the tool output."""
    try:
        path = resources.files("ietf_llm").joinpath(f"data/skills/{skill}/SKILL.md")
        return _strip_frontmatter(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return (
            f"(the {skill} skill is missing from the installed package — "
            "try reinstalling: pipx install --force ietf-llm)"
        )


def tool_read_interpretation_norms() -> str:
    """Return the `ietf-interpreting` skill body — interpretive norms for
    reading a corpus (consensus, who-speaks-for-whom, list-vs-meeting).

    The norms also ship as a standalone skill that auto-triggers on
    "what did the WG decide / who supports what"; this tool is the MCP
    surface for the same content, pulled on demand by clients that reach
    it as a tool rather than a skill.
    """
    return _read_bundled_skill_body("ietf-interpreting")


def tool_read_participation_norms() -> str:
    """Return the `ietf-contributing` skill body — norms for helping a human
    contribute to a corpus (drafting list mail, GitHub issues/comments,
    other discussion), the write-side companion to the reading norms.

    Pulled on demand when the question shifts from interpreting the
    record to composing something that goes into it under a person's
    name. Authoring Internet-Drafts is out of scope of the doc.
    """
    return _read_bundled_skill_body("ietf-contributing")


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
            f"an older version — {gather_suggestion(wg, purpose='to rebuild', force=True)}.",
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


# digests/message_citations.md structure (gather.message_citations):
#   ## Resolved (target gathered here)
#   ### `threads/<file>.md` [chunk N] — Sender, DATE — "Subject"
#   cited by:
#   - `threads/<src>.md` [chunk M] — _context_
#   ## External (not gathered in this corpus)
#   ### https://... (gather `list`?)
#   cited by:
#   - `threads/<src>.md` [chunk M] — _context_
_MC_RESOLVED_HDR_RE = re.compile(
    r"^### `(?P<f>[^`]+)` \[chunk (?P<n>\d+)\] — (?P<rest>.*)$"
)
_MC_EXTERNAL_HDR_RE = re.compile(r"^### (?P<url>https?://\S+)(?P<hint>.*)$")
_MC_BULLET_RE = re.compile(
    r"^- `(?P<sf>[^`]+)` \[chunk (?P<m>\d+)\] — _(?P<ctx>.*)_\s*$"
)


def _parse_message_citations(
    text: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse message_citations.md into (resolved_edges, external_edges).

    resolved edge: {tgt_file, tgt_chunk, tgt_rest, src_file, src_chunk, ctx}
    external edge: {url, hint, src_file, src_chunk, ctx}
    """
    resolved: List[Dict[str, Any]] = []
    external: List[Dict[str, Any]] = []
    section = ""  # "resolved" | "external"
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith("## Resolved"):
            section, cur = "resolved", None
            continue
        if line.startswith("## External"):
            section, cur = "external", None
            continue
        res_hdr = _MC_RESOLVED_HDR_RE.match(line)
        if res_hdr and section == "resolved":
            cur = {
                "tgt_file": res_hdr.group("f"),
                "tgt_chunk": int(res_hdr.group("n")),
                "tgt_rest": res_hdr.group("rest").strip(),
            }
            continue
        ext_hdr = _MC_EXTERNAL_HDR_RE.match(line)
        if ext_hdr and section == "external":
            cur = {
                "url": ext_hdr.group("url"),
                "hint": ext_hdr.group("hint").strip(),
            }
            continue
        bullet = _MC_BULLET_RE.match(line)
        if bullet and cur is not None:
            edge = {
                "src_file": bullet.group("sf"),
                "src_chunk": int(bullet.group("m")),
                "ctx": bullet.group("ctx").strip(),
            }
            if section == "resolved":
                resolved.append({**cur, **edge})
            else:
                external.append({**cur, **edge})
    return resolved, external


@_requires_corpus
def tool_find_message_citations(
    wg: str, file: str, chunk_idx: Optional[int] = None
) -> str:
    """Walk the message reference graph for a thread / issue file.

    Mailing-list messages routinely cite *other messages* by archive
    permalink — an appeal links the post being appealed, a split thread
    links the message it forked from, a reply footnotes the one it
    answers. The gather step resolves those links (against the
    `Archived-At:` permalinks on the gathered messages) into
    `digests/message_citations.md`; this tool reads the graph for one
    file and returns:

      - **Inbound** — other messages that cite this file (optionally the
        specific message `chunk_idx`). The reverse index `fetch_by_url`
        can't give you: "who else references this message / decision?".
      - **Outbound** — the archive links this file cites, each resolved
        to its local `file` / `chunk_idx` (pivot with `read_file_section`
        / `get_chunk_text`) or flagged external (a message not gathered
        here — often another list; gather it and retry).

    `file` is a corpus-relative path like `threads/<file>.md` or
    `issues/<org-repo>/<n>.md`. Pass `chunk_idx` to scope to one message.

    Within-scheme resolution only: a `mailarchive.ietf.org` token is not
    bridged to a stored `www.w3.org/mid` Message-ID, so on a list that
    stamps one scheme while bodies cite the other, real targets can show
    as external. To fetch a single URL directly, use `fetch_by_url`.
    """
    cache = _files_dir(wg)
    md = digest_path(cache, "message_citations")
    if not os.path.isfile(md):
        return _with_freshness(
            wg,
            f"No message-citations digest for {wg}. Either no thread / "
            "issue files cite an archive permalink, or this corpus was "
            "gathered with an older version — "
            f"{gather_suggestion(wg, purpose='to rebuild', force=True)}.",
        )
    try:
        with open(md, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return f"Couldn't read message-citations digest for {wg}."
    target = file.strip().strip("`").lstrip("./")
    resolved, external = _parse_message_citations(text)

    def _match(path: str, chunk: int) -> bool:
        return path == target and (chunk_idx is None or chunk == chunk_idx)

    inbound = [e for e in resolved if _match(e["tgt_file"], e["tgt_chunk"])]
    out_resolved = [e for e in resolved if _match(e["src_file"], e["src_chunk"])]
    out_external = [e for e in external if _match(e["src_file"], e["src_chunk"])]

    if not inbound and not out_resolved and not out_external:
        scope = f" [chunk {chunk_idx}]" if chunk_idx is not None else ""
        return _with_freshness(
            wg,
            f"No message citations recorded for `{target}`{scope} in {wg} "
            "— it neither cites an archive permalink nor is cited by one "
            "that resolves here. (The graph only covers archive-URL links "
            "between cached messages.)",
        )

    scope = f" [chunk {chunk_idx}]" if chunk_idx is not None else ""
    lines = [f"# Message citations for `{target}`{scope} in {wg}\n"]
    if inbound:
        lines.append("## Inbound — messages that cite this\n")
        for edge in sorted(inbound, key=lambda x: (x["src_file"], x["src_chunk"])):
            tgt = (
                "" if chunk_idx is not None else f" → cites [chunk {edge['tgt_chunk']}]"
            )
            lines.append(
                f"- `{edge['src_file']}` [chunk {edge['src_chunk']}]{tgt} "
                f"— _{edge['ctx']}_"
            )
        lines.append("")
    if out_resolved or out_external:
        lines.append("## Outbound — archive links this cites\n")
        for edge in sorted(out_resolved, key=lambda x: (x["tgt_file"], x["tgt_chunk"])):
            lines.append(
                f"- → `{edge['tgt_file']}` [chunk {edge['tgt_chunk']}] "
                f"({edge['tgt_rest']}) — _{edge['ctx']}_"
            )
        for edge in sorted(out_external, key=lambda x: x["url"]):
            hint = f" {edge['hint']}" if edge["hint"] else ""
            lines.append(f"- → external {edge['url']}{hint} — _{edge['ctx']}_")
        lines.append("")
    return _with_freshness(wg, "\n".join(lines))


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


def _render_hits(hits: List[Any], k: int, group_by: Optional[str]) -> str:
    """Render a list of `Hit`s to the text block shown to the caller.

    Shared by `tool_search` and `tool_find_related`. `group_by="file"`
    collapses to one row per file; otherwise the per-chunk view, led by a
    result-set state summary when every hit shares one issue state.
    """
    if group_by == "file":
        return _render_file_grouped(hits, k)
    hits = hits[:k]
    lines: List[str] = []
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
    return "\n".join(lines)


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
    # Seed `seen` with the root so a malformed self-reply marker
    # (`[5] … (reply to [5])` → graph[5] = [5]) can't list the root as its own
    # descendant, and the walk stays cycle-safe regardless of marker damage.
    seen = {root}
    queue = [child for child in graph.get(root, []) if child not in seen]
    seen.update(queue)
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


# Grounding-frame thresholds — the point past which a thread is too big to
# safely read level-of-support / individual positions off narrative snippets.
# Deliberately low at 20 msgs / 8 participants: a TLS-sized bar (say 40/15)
# clears routinely in a busy group but a quiet WG would never reach it, so
# these lower values are what make the nudge fire across the range of groups
# while still staying quiet on a routine short thread.
_GROUNDING_MIN_MSGS = 20
_GROUNDING_MIN_PARTICIPANTS = 8


def _first_int(cell: str) -> int:
    """First integer in a digest table cell (`"325"`, `"62"`), 0 if none."""
    match = re.search(r"\d+", cell or "")
    return int(match.group()) if match else 0


def _biggest_grounding_thread(
    wg: str, files: List[str]
) -> Optional[Tuple[int, int, str]]:
    """The largest thread among `files` that clears the grounding threshold,
    as `(msgs, participants, file)` — or None. Sizes come from the threads
    digest; only thread/issue files count, deduped, and an unparseable size
    cell reads as 0 (so a malformed digest row fails the threshold rather
    than crashing)."""
    sizes = _thread_sizes(wg)
    if not sizes:
        return None
    seen: set[str] = set()
    best: Optional[Tuple[int, int, str]] = None
    best_msgs = -1
    for file in files:
        if file in seen or file not in sizes or not file_supports_tally(file):
            continue
        seen.add(file)
        msgs = _first_int(sizes[file][0])
        parts = _first_int(sizes[file][1])
        if msgs < _GROUNDING_MIN_MSGS and parts < _GROUNDING_MIN_PARTICIPANTS:
            continue
        if msgs > best_msgs:
            best_msgs = msgs
            best = (msgs, parts, file)
    return best


def _grounding_frame(wg: str, files: List[str]) -> str:
    """Interpretive frame prepended to narrative / search output when results
    touch a thread big enough to mislead. It states the principle *inline* —
    narrative is what was said, not what was decided; IETF consensus is
    chair-declared, not counted — so the frame is already in context before
    the caller reads the messages, with no separate tool call to skip (the
    skippable pointer was the thing that didn't work). Empty when no
    qualifying thread is present."""
    best = _biggest_grounding_thread(wg, files)
    if best is None:
        return ""
    msgs, parts, file = best
    return (
        "> **Before characterising any decision, position, or level of "
        "support from what follows:** this is *narrative* — what participants "
        "said, not what the group decided. IETF consensus is "
        "**chair-declared, not vote-counted**, so do not infer it from these "
        "messages or from a `+1`/`-1` count (a keyword heuristic, not a "
        "measure of support). Anchor any such claim to the chair's own "
        "words — a consensus call / WGLC / closure (`tally_positions` "
        "surfaces these in its **Chair statements** section), a closed-issue "
        'resolution (`state="closed"`), or '
        f'`search_corpus("{wg}", "...", role="Chair")`. Full procedure: '
        f"`read_ietf_interpretation_norms`. _(Flagged by `{file}` — "
        f"{msgs} msgs, {parts} participants.)_\n"
    )


def _participation_nudge(files: "str | List[str]") -> str:
    """The write-side mirror of `_grounding_frame`.

    The read tools surface quotable raw material — thread / issue messages.
    The instant a caller has that in hand is the moment a drafting decision
    gets made, and the last point inside the server before they leave to
    compose somewhere the always-on instructions no longer reassert
    themselves. So, symmetric to the read-side consensus banner, flag the
    *write-side* gate right here, at the point of material acquisition — not
    only in the server instructions a model has already scrolled past.

    Fires only when the material is `threads/` or `issues/` content (what
    gets quoted into a reply), never for drafts / RFCs / digests. A nudge,
    not enforcement. Empty when no such file is in view."""
    paths = [files] if isinstance(files, str) else files
    if not any(p.lower().startswith(("threads/", "issues/")) for p in paths):
        return ""
    return (
        "> ✍ **About to draft a contribution from this?** Before you write "
        "list mail, a GitHub issue or comment, or any reply that goes into "
        "the record under a participant's name, you MUST call "
        "`read_ietf_participation_norms` first — the human is accountable and "
        "sends; you only draft. Reading the corpus is not the same as "
        "contributing to it. _(Ignore if you are only querying — this fires "
        "on raw message material, which is what a reply quotes.)_"
    )


def _append_participation_nudge(
    files: "str | List[str]", body: str, *, enabled: bool = True
) -> str:
    """Append the write-side nudge as a footer to `body`, when one fires and
    `enabled`. `enabled=False` lets a tool that fans out to another read tool
    (get_chunks_batch → get_chunk) suppress the inner per-chunk footer and emit
    a single one at its own boundary instead."""
    if not enabled:
        return body
    nudge = _participation_nudge(files)
    return f"{body}\n\n---\n\n{nudge}" if nudge else body


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
        no_index = (
            f"the corpus may have no search index — re-gather it with {_regather_call(wg)}"
            if gather_enabled()
            else f"has `ietf-llm {wg} --embed` been run?"
        )
        return _with_freshness(wg, f"(no results for {query!r} — {no_index})")

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
    # Interpretive frame FIRST, before the narrative — read_topic is the
    # narrative-reconstruction tool, and once a caller is deep in 60 messages
    # a margin note won't pull them out. Fires only for a large/contentious
    # thread; scans all matched thread/issue files so a big thread shown only
    # in part still triggers it.
    frame = _grounding_frame(wg, [h.file for h in thread_issue_hits])
    if frame:
        out.append(frame.rstrip("\n"))
        out.append("")
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

    # Write-side nudge, after the narrative: this is message material a reply
    # would quote, so flag the participation-norms gate at the point the
    # drafting decision is made (mirrors the read-side frame at the top).
    nudge = _participation_nudge(files)
    if nudge:
        out.append("---")
        out.append("")
        out.append(nudge)
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
    return _append_participation_nudge(file, _with_freshness(wg, "\n".join(out)))


@_requires_corpus
def tool_tally_positions(wg: str, file: str) -> str:
    """Surface a thread / issue file's procedural call, polls, and (cautiously)
    a position tally.

    Best for two things: **chair statements** — messages from a chair carrying
    procedural language (`rough consensus`, `consensus call`, `WGLC`,
    `adopting`, closure) surfaced at the top, the load-bearing posts in a long
    thread — and **option polls** (`option N` / `#N` / `I prefer N`), counted
    per choice.

    The support/oppose tally is a **keyword heuristic** (`+1`, `-1`,
    `I support`, `I object`, `LGTM`, `DISCUSS`, near the message start). It is
    low-recall and **misses prose-form positions**, which is how IETF
    participants usually argue — so a contentious thread can tally as
    no-position. A coverage percentage is reported; when it is low the counts
    are withheld and a warning points you to the chair statements and the
    messages themselves. Never quote a low-coverage count as the level of
    support. Each row is enriched with role / affiliation from the people
    digest when known.
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
    diversify: bool = True,
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
        # `group_by="file"` is already a coarse diversification (one row
        # per file), so MMR on top of it is redundant churn — let the
        # rollup do the de-duplication and keep the per-file best chunks.
        diversify=diversify and group_by != "file",
        verbose=Verbosity.QUIET,
    )
    if not hits:
        no_index = (
            f"the corpus may have no search index — re-gather it with {_regather_call(wg)}"
            if gather_enabled()
            else f"has `ietf-llm {wg} --embed` been run?"
        )
        return _with_freshness(wg, f"(no results — {no_index})")
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
    # Grounding frame at the TOP — read before the hits, with no separate
    # tool call to skip. Only the open-ended search path gets it (not the
    # shared `_render_hits`, which `tool_find_related` also uses). Fires only
    # when a result touches a thread big enough that its consensus / positions
    # shouldn't be read off snippets. We scan the chunk-level `hits[:k]`; in
    # `group_by="file"` mode the rendered rows collapse to files, so the named
    # thread may not be a displayed row — harmless, the frame is valid for the
    # result set either way.
    frame = _grounding_frame(wg, [h.file for h in hits[:k]])
    body = _render_hits(hits, k, group_by) + note
    return _with_freshness(wg, f"{frame}\n{body}" if frame else body)


def tool_find_related(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    wg: str,
    file: str,
    chunk_idx: int,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    group_by: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    diversify: bool = True,
    collapse_versions: bool = True,
) -> str:
    for field, value in (("since", since), ("until", until)):
        date_error = _invalid_date_message(value, field)
        if date_error:
            return date_error
    try:
        k = max(1, min(int(k), _MAX_SEARCH_K))
    except (TypeError, ValueError):
        k = 10
    try:
        chunk_idx = int(chunk_idx)
    except (TypeError, ValueError):
        return f"(invalid chunk_idx {chunk_idx!r} — must be an integer)"
    # Over-fetch when we will thin the results — `group_by="file"` rolls up
    # per file, and `collapse_versions` drops older draft revs — so the
    # final list still reaches `k` distinct items (mirrors tool_search).
    over_fetch = group_by == "file" or collapse_versions
    fetch_k = max(k * 4, 20) if over_fetch else k
    hits = related(
        wg,
        file,
        chunk_idx,
        k=fetch_k,
        file_pattern=file_pattern,
        since=since,
        until=until,
        label=label,
        state=state,
        snippet_chars=snippet_chars,
        diversify=diversify and group_by != "file",
        verbose=Verbosity.QUIET,
    )
    if not hits:
        no_index = (
            f"no search index, or no chunk {chunk_idx} in {file} — "
            f"re-gather with {_regather_call(wg)}"
            if gather_enabled()
            else f"no chunk {chunk_idx} in {file}, or `ietf-llm {wg} --embed` "
            "has not been run"
        )
        return _with_freshness(wg, f"(no related chunks — {no_index})")
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
    return _with_freshness(wg, _render_hits(hits, k, group_by) + note)


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
        how = (
            're-gather each with `start_gather(corpus="<name>", force=True)`'
            if gather_enabled()
            else "run `ietf-llm <name>`"
        )
        skip_notes.append(f"no embedding index — {how}: " + ", ".join(no_index))
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


def tool_which_corpus(query: str, limit: int = 8) -> str:
    clean = (query or "").strip()
    if not clean:
        return (
            "which_corpus needs a question or topic to route, e.g. "
            '`which_corpus("0-RTT replay protection")`.'
        )
    result = route(clean, limit=limit)
    if result.error == "embed-failed":
        return (
            f"Could not embed the query with model `{result.model_id}` to route it. "
            "The embedding backend may be unavailable; try `find_efforts` "
            "(keyword-based) instead."
        )
    if (
        not result.matches
        and not result.skipped_other_model
        and not result.no_centroids
    ):
        return (
            "No gathered corpus has a topic map yet — routing centroids populate "
            "at gather time. Re-gather a corpus (`ietf-llm <name>`), then retry. "
            "Meanwhile `find_efforts(topic)` discovers efforts to gather and "
            "`list_corpora` shows what is cached."
        )

    lines: List[str] = []
    if result.confident:
        lines.append(
            f"**Which corpus** for {clean!r} — gathered corpora ranked by "
            f"topic-centroid similarity (embedding model `{result.model_id}`):"
        )
        lines.append("")
        for i, match in enumerate(result.matches, 1):
            weak = "" if match.score >= DEFAULT_MIN_SCORE else "  _(below floor)_"
            lines.append(f"{i}. **{match.corpus}** — {match.score:.3f}{weak}")
        top = result.matches[0].corpus
        lines.append("")
        lines.append(
            f'Search the best fit with `search_corpus("{top}", "...")`, or compare '
            'a few with `search_corpora([...], "...")`. These are routing hints '
            "(topic proximity), not proof the answer is there — confirm by searching."
        )
    else:
        closest = result.matches[0] if result.matches else None
        if closest is not None:
            lines.append(
                f"No confident match for {clean!r} among gathered corpora "
                f"(closest: **{closest.corpus}** {closest.score:.3f}, below the "
                f"{DEFAULT_MIN_SCORE:.2f} confidence floor)."
            )
        else:
            lines.append(f"No confident match for {clean!r} among gathered corpora.")
        lines.append("")
        lines.append(
            "The right effort may not be gathered yet — try "
            f"`find_efforts({clean!r})` to discover candidates and gather one, or "
            "the question may be off-topic for what is cached. Don't force a search "
            "against a low-confidence guess."
        )
        if result.matches:
            lines.append("")
            lines.append("_Closest (weak) matches:_")
            for match in result.matches:
                lines.append(f"- {match.corpus} — {match.score:.3f}")

    if result.skipped_other_model:
        lines.append(
            f"\n_Scored only corpora on the majority embedding model "
            f"`{result.model_id}`; not comparable, so skipped: "
            f"{', '.join(result.skipped_other_model)}._"
        )
    if result.no_centroids:
        lines.append(
            "\n_No topic map yet (re-gather to include in routing): "
            f"{', '.join(result.no_centroids)}._"
        )
    return "\n".join(lines)


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
def tool_fetch_by_url(wg: str, url: str) -> str:
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
            f"No cached chunk for {url}. fetch_by_url resolves the URL forms "
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
        # Busy timeout so this read waits out a concurrent gather write
        # instead of erroring with "database is locked". A sqlite3 connection
        # used as a context manager only scopes the transaction, not the
        # connection, so close it explicitly to avoid leaking one fd per
        # corpus scanned before a model is found.
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
            if row:
                model_name = row[0]
                break
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
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
    """Read the bundled ietf-llm skill's SKILL.md, returning its body
    (frontmatter stripped).

    Passed to FastMCP as the server-level `instructions` field, which
    MCP-compliant clients surface to the model as system-prompt
    context. Source-of-truth-once: this is the same `ietf-llm` skill
    `--install-skills` copies into Claude Code, so non-Claude harnesses
    (Codex, Gemini, Cursor, Zed, opencode, …) see the same routing rules
    and IETF norms without us maintaining a parallel guidance string.

    YAML frontmatter (the `---` block at the top with `name:` /
    `description:`) is stripped — it's skill metadata, not guidance.
    Returns None if the file is missing (shouldn't happen for an
    installed package, but a defensive None lets the server come up
    anyway).
    """
    try:
        skill_path = resources.files("ietf_llm").joinpath(
            "data/skills/ietf-llm/SKILL.md"
        )
        text = skill_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    return _strip_frontmatter(text)


# Named capability flags a skill or downstream tool can gate on, so it
# checks "does feature X exist" instead of comparing version numbers
# (brittle the moment a feature is backported or renamed). The version
# itself is the canonical protocol identity in `serverInfo.version`; this
# list is the agent-readable feature set, surfaced in `instructions` (the
# model never sees `serverInfo`). Add a flag here when you land a feature a
# skill might depend on; never remove one without a real capability change.
SERVER_FEATURES: tuple[str, ...] = (
    "live-lookup",  # overview(live=), draft_status, draft_authors, meeting_sessions
    "label-digest",  # read_digest(label=) — label-filtered issue/thread digests
)


def _capability_footer() -> str:
    """A single agent-readable line stating the running server's version and
    capability flags, appended to the MCP `instructions` field so a skill can
    confirm a feature is present (and tell the user to upgrade if not).

    The model never sees `serverInfo.version` — that reaches the host, not the
    prompt — so feature-gating lives here, in text the client surfaces as
    system context. Gate on the named flags, not on the version number."""
    features = ", ".join(SERVER_FEATURES)
    return (
        f"\n\n---\n\n_ietf-llm server version {__version__}; "
        f"features: {features}. A skill that needs a feature should check "
        "for its flag here and ask the user to upgrade ietf-llm if absent._"
    )


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
    serve_metrics.adjust_inflight(1)
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
        serve_metrics.adjust_inflight(-1)
        elapsed = time.monotonic() - t0
        _debug_log.log_event(
            req_id,
            "offload_end",
            status=status,
            elapsed=round(elapsed, 6),
        )
        # RED per tool for the /metrics scrape (issue #40). `status` is
        # "ok" on success; "timeout"/"exception" both count as errors, and
        # a "timeout" is additionally counted on its own series so a
        # deadline hit (slow upstream / cold embedding / cache contention)
        # is distinguishable from a raised exception (a bug).
        serve_metrics.record_tool(
            getattr(fn, "__name__", "tool"),
            elapsed,
            error=status != "ok",
            timeout=status == "timeout",
        )
        # Per-request access record on the structured stream. /metrics shows
        # the aggregate; this is the per-call line a hosted deployment needs to
        # query individual requests by field (tool / status / duration). Gated
        # by the serve verbosity, so stdio/local stays silent by default while
        # the HTTP container emits one queryable line per tool call.
        tool_name = getattr(fn, "__name__", "tool")
        log(
            f"tool {tool_name} {status} {elapsed * 1000:.0f}ms",
            _log_verbosity(),
            level=LogLevel.STATUS,
            fields={
                "event": "tool_call",
                "tool": tool_name,
                "status": status,
                "duration_ms": round(elapsed * 1000, 1),
            },
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
    """True when the gather tools are active.

    The default tracks the transport (set in `main` via
    `freshness.set_gather_default`): a local stdio server defaults gather
    *on*, while the shared HTTP deployment defaults it *off* to preserve the
    read-only / no-network guarantee. Gather writes to the cache and reaches
    the network, which the rest of the server never does — but a local stdio
    user can already run `ietf-llm` against the same cache, so withholding the
    tool there only adds friction. `IETF_LLM_ENABLE_GATHER` overrides either
    way (e.g. set it falsy on a stdio server pointed at a read-only cache).

    Delegates to `freshness.gather_enabled` (one source of truth) so the
    user-facing gather hints in other modules name the same path.
    """
    return gather_enabled()


# How long `start_gather(wait=...)` blocks for a gather to finish before
# falling back to the progress-and-poll reply. Blocking is the default (the
# dominant flow is gather-then-read, one call), and `wait=0` restores
# fire-and-forget. The budget is always clamped under the `_offload` deadline
# so the wait loop returns its own "still running, poll" message rather than
# the generic tool-timeout firing first.
_GATHER_WAIT_DEFAULT = 90.0
_GATHER_WAIT_MARGIN = 15.0
_GATHER_POLL_INTERVAL = 2.0
_TERMINAL_GATHER_STATES = frozenset({"done", "failed", "cancelled", "interrupted"})


def _gather_wait_budget(requested: Optional[float], elapsed: float = 0.0) -> float:
    """Effective seconds to block in the gather tools, clamped under the
    offload deadline.

    `None` → the default budget (blocking by default); `<= 0` → don't wait
    (fire-and-forget). A positive request is honoured but clamped to leave
    headroom under `IETF_LLM_TOOL_TIMEOUT`, so the wait loop always gets to
    return its own progress reply instead of the offload deadline cancelling
    the call first.

    `elapsed` is the wall-clock already spent in this tool call before the wait
    begins — e.g. `gather_runner.start()`, which on the cloud backend does S3
    round-trips for the lease / status / enqueue. The clamp is against
    *remaining* time (`timeout - elapsed - margin`), not the static budget, so
    a slow pre-wait phase can't push the wait past the deadline it is meant to
    stay under (the clamp shrinks, to 0 if no headroom is left).
    """
    base = _GATHER_WAIT_DEFAULT if requested is None else float(requested)
    if base <= 0:
        return 0.0
    timeout = _tool_timeout_seconds()
    if timeout > 0:
        base = min(base, max(0.0, timeout - elapsed - _GATHER_WAIT_MARGIN))
    return base


def _await_gather(corpus: str, budget: float) -> Optional[Dict[str, Any]]:
    """Poll the gather status until it is terminal or `budget` seconds elapse;
    return the last status seen (or None if none is recorded).

    Runs inside the `_offload` worker thread, so the blocking `time.sleep` is
    off the event loop and the server stays responsive to other requests.
    """
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    deadline = time.monotonic() + budget
    status = gather_runner.read_status(corpus)
    while True:
        state = status.get("state") if status else None
        if state in _TERMINAL_GATHER_STATES:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status
        time.sleep(min(_GATHER_POLL_INTERVAL, remaining))
        status = gather_runner.read_status(corpus)


def _format_waited_result(status: Dict[str, Any], corpus: str) -> str:
    """Render the reply after `start_gather` blocked through to a terminal
    gather state."""
    line = _format_gather_status(status)
    if status.get("state") == "done":
        line += (
            f"\nReady — query '{corpus}' now with `overview`, `read_digest`, "
            "or `search_corpus`."
        )
    return line


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
    wait: Optional[float] = None,
) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    wait_started = time.monotonic()
    corpus = (corpus or "").strip()
    if not corpus:
        return "Provide a corpus name to gather (e.g. a WG shortname like `tls`)."
    if not gather_runner.valid_corpus_name(corpus):
        return (
            f"'{corpus}' is not a valid corpus name. Use letters, digits, "
            "'.', '-' or '_' (no path separators or spaces), starting with a "
            "letter or digit."
        )
    months_error = months_request_error(months, force)
    if months_error:
        return months_error
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
    out = _format_start_result(result, corpus)
    caution = months_request_caution(months)
    if result.get("started") and caution:
        out = f"{out} {caution}"
    budget = _gather_wait_budget(wait, elapsed=time.monotonic() - wait_started)
    in_progress = result.get("started") or result.get("reason") == "already running"
    if budget > 0 and in_progress:
        final = _await_gather(corpus, budget)
        if final and final.get("state") in _TERMINAL_GATHER_STATES:
            return _format_waited_result(final, corpus)
        return (
            f"Waited ~{int(budget)}s; the gather is still in progress. "
            f"⚠ Reads before it reports `done` may be stale or partial — search "
            f"and digests are built in the *final* stages, and a re-gather keeps "
            f"serving the previous snapshot until it finishes. To block until "
            f'done, call `gather_status(corpus="{corpus}", wait=60)` rather than '
            f"reading now. {out}"
        )
    return out


def _format_start_result(result: Dict[str, Any], corpus: str) -> str:
    """Render `gather_runner.start`'s result dict as the tool's reply."""
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
        if reason == "queue full":
            return (
                f"This host's gather queue is full, so '{corpus}' was not "
                "accepted. Too many gathers are already pending — wait for some "
                "to finish (`gather_status()`), then retry."
            )
        return (
            f"A gather for '{corpus}' is already running. "
            f'Poll `gather_status(corpus="{corpus}")` for progress.'
        )
    token = result.get("cancel_token")
    stop_hint = (
        f' To stop it, call `stop_gather(corpus="{corpus}", token="{token}")` '
        "(this token is the only way to cancel it — keep it for this gather)."
        if token
        else ""
    )
    if result.get("queued_behind"):
        ahead = result["queued_behind"]
        return (
            f"Queued '{corpus}' for gathering ({ahead} gather"
            f"{'s' if ahead != 1 else ''} ahead of it). Gathers are capped to a "
            "few at once (per host and across the deployment) to stay polite to "
            f"upstreams, so it starts when a slot frees. Poll "
            f'`gather_status(corpus="{corpus}")`.{stop_hint}'
        )
    timing = (
        "re-gathers are usually quick — only new material is fetched"
        if _corpus_exists(corpus)
        else "a first gather of a corpus can take minutes"
    )
    return (
        f"Started gathering '{corpus}' in the background ({timing}). Poll "
        f'`gather_status(corpus="{corpus}")` for stage-level progress; the '
        f"corpus is queryable once it reports `done` (reads before then are "
        f"stale or partial).{stop_hint}"
    )


def tool_gather_status(
    corpus: Optional[str] = None, wait: Optional[float] = None
) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    wait_started = time.monotonic()
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
        # Immediate by default (unlike start_gather); a positive `wait` blocks
        # for a still-running gather to finish, clamped under the tool deadline
        # (against time already spent in the status read above).
        budget = _gather_wait_budget(wait or 0, elapsed=time.monotonic() - wait_started)
        if budget > 0 and status.get("state") not in _TERMINAL_GATHER_STATES:
            status = _await_gather(corpus, budget) or status
        return _format_gather_status(status)
    statuses = gather_runner.all_statuses()
    if not statuses:
        return "No gathers have been recorded yet."
    return "\n".join(_format_gather_status(s) for s in statuses)


def tool_stop_gather(corpus: str, token: str) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel

    corpus = (corpus or "").strip()
    if not corpus:
        return "Provide the corpus name of the gather to stop."
    if not gather_runner.valid_corpus_name(corpus):
        return f"'{corpus}' is not a valid corpus name."
    return _format_stop_result(
        gather_runner.request_stop(corpus, (token or "").strip()), corpus
    )


def _format_stop_result(result: Dict[str, Any], corpus: str) -> str:
    """Render `gather_runner.request_stop`'s result dict as the tool's reply."""
    if result.get("stopped"):
        return (
            f"Requested stop for '{corpus}'. It ends at the next stage boundary "
            f'(download batch / stage); poll `gather_status(corpus="{corpus}")` '
            "until it reports `cancelled`. The partial gather is not published, "
            "so the corpus keeps its previous contents (if any)."
        )
    reason = result.get("reason")
    if reason == "not running":
        state = result.get("state")
        tail = f" (it is `{state}`)" if state else ""
        return f"No gather is running for '{corpus}'{tail}, so nothing to stop."
    if reason == "bad token":
        return (
            f"That token does not match the running gather for '{corpus}', so it "
            "was not stopped. Use the token returned by the `start_gather` call "
            "that began it."
        )
    return f"'{corpus}' is not a valid corpus name."


def _format_gather_status(status: Dict[str, Any]) -> str:
    """One compact line for a gather status record."""
    corpus = status.get("corpus", "?")
    state = status.get("state", "?")
    parts = [f"**{corpus}** — {state}"]
    if state == "queued":
        parts.append("waiting for a gather slot (gathers are capped to a few at once)")
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
        detail = status.get("stage_detail")
        if detail:
            parts.append(str(detail))
        if status.get("cancel_requested"):
            parts.append("stop requested; finishing the current stage")
    # An interrupted gather never finished, so its start->now span isn't a
    # meaningful "elapsed" (it would grow on every poll); omit it.
    if state != "interrupted":
        elapsed = _gather_elapsed(status)
        if elapsed:
            parts.append(elapsed)
    if state == "interrupted":
        parts.append("the gather process ended before completion; re-run it")
    if state == "cancelled":
        parts.append("stopped on request; partial gather discarded, re-run to retry")
    if state == "failed" and status.get("error"):
        parts.append(f"error: {status['error']}")
    line = " · ".join(parts)
    # Pipeline notes (e.g. GitHub repos auto-tracked, or that discovery was
    # throttled) printed under the status line so the client can act on them.
    notes = status.get("notes")
    if isinstance(notes, list) and notes:
        line += "".join(f"\n  - {note}" for note in notes)
    return line


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


def tool_suggest_github_repos(corpus: str) -> str:
    from . import gather_runner  # pylint: disable=import-outside-toplevel
    from .gather.repo_discovery import (  # pylint: disable=import-outside-toplevel
        discover_group_repos,
        format_discovery,
    )

    corpus = (corpus or "").strip()
    if not corpus:
        return "Provide a Working Group shortname (e.g. `tls`) to discover repos for."
    if not gather_runner.valid_corpus_name(corpus):
        return f"'{corpus}' is not a valid corpus name."
    return format_discovery(discover_group_repos(corpus))


def _find_latest_draft_file(name: str) -> Optional[str]:
    """Locate the highest-revision cached `.txt` for a draft across all corpora.

    A draft name (e.g. `draft-ietf-httpbis-foo`) implies its WG but can be
    cached under more than one corpus; this scans every gathered corpus's
    `drafts/` dir and returns the newest revision found anywhere, or None.
    Read-only — uses the same per-corpus files dir the read tools resolve.
    """
    base = normalize_draft_name(name)
    pattern = re.compile(rf"^{re.escape(base)}-(\d+)\.txt$")
    best_version = -1
    best_path: Optional[str] = None
    for wg in _list_wgs():
        try:
            cache = _files_dir(wg)
        except FileNotFoundError:
            continue
        ddir = drafts_dir(cache)
        if not os.path.isdir(ddir):
            continue
        for fname in os.listdir(ddir):
            match = pattern.match(fname.lower())
            if not match:
                continue
            version = int(match.group(1))
            if version > best_version:
                best_version = version
                best_path = os.path.join(ddir, fname)
    return best_path


def tool_draft_authors(name: str) -> str:
    """Render a draft's authors/editors with contact emails, from the cache.

    Reads the Authors' Addresses section of the newest cached revision (the
    same text gather already parses) — no network. Returns what the draft
    itself records; a chair may know a better working address from mail and
    can override.
    """
    from .gather.draft_authors import (  # pylint: disable=import-outside-toplevel
        parse_authors,
    )

    name = (name or "").strip()
    if not name:
        return (
            "Provide a draft name, e.g. `draft-ietf-httpbis-resumable-upload` "
            "(the version suffix is optional)."
        )
    base = normalize_draft_name(name)
    path = _find_latest_draft_file(name)
    if path is None:
        return (
            f"No cached copy of `{base}` in any gathered corpus. Gather the "
            "owning corpus (its WG) first, then retry — author contacts are "
            "read from the cached draft text."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return f"Could not read the cached draft `{os.path.basename(path)}`."

    authors = parse_authors(text)
    fname = os.path.basename(path)
    if not authors:
        return (
            f"No parseable Authors' Addresses section in the cached `{fname}` — "
            "read the draft's tail directly with `read_file_section`."
        )
    lines = [
        f"# Authors of {base}\n",
        f"_From the Authors' Addresses section of the cached `{fname}`. "
        "These are the draft-stated addresses; a chair may have a better "
        "working address from mail._\n",
    ]
    for author in authors:
        role = "editor" if author.is_editor else "author"
        org = f", {author.organization}" if author.organization else ""
        email = author.email or "_no email listed_"
        lines.append(f"- **{author.name}** ({role}){org} — {email}")
    return "\n".join(lines)


#: Line cap for a single gathered minutes read, so a pathological transcript-
#: sized minutes file cannot blow the context window in one call.
_MINUTES_MAX_LINES = 2000


def _read_text_capped(path: str, max_lines: int) -> str:
    """Read a file, truncating past `max_lines` with a pointer to page the
    rest via `read_file_section`. Empty string if unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    if len(lines) > max_lines:
        return "".join(lines[:max_lines]) + (
            f"\n\n_(truncated at {max_lines} lines — read further with "
            "`read_file_section`)_\n"
        )
    return "".join(lines)


def _session_date(cache: str, code: str) -> str:
    """Best-effort session date from a meeting's minutes header, or ''."""
    try:
        with open(minutes_path(cache, code), encoding="utf-8") as handle:
            head = handle.read(2000)
    except OSError:
        return ""
    match = re.search(r"(?im)^\s*Date:\s*(\d{4}-\d{2}-\d{2})", head)
    return match.group(1) if match else ""


def _dir_md_count(directory: str) -> int:
    """Number of `.md` files directly in `directory` (0 if it is absent)."""
    if not os.path.isdir(directory):
        return 0
    return len([f for f in os.listdir(directory) if f.endswith(".md")])


def _session_artifacts(cache: str, code: str) -> str:
    """Compact 'minutes · agenda · N transcripts · N polls' inventory."""
    parts: List[str] = []
    if os.path.isfile(minutes_path(cache, code)):
        parts.append("minutes")
    if os.path.isfile(agenda_path(cache, code)):
        parts.append("agenda")
    n_tx = _dir_md_count(transcripts_dir(cache, code))
    if n_tx:
        parts.append(f"{n_tx} transcript{'s' if n_tx != 1 else ''}")
    n_polls = _dir_md_count(polls_dir(cache, code))
    if n_polls:
        parts.append(f"{n_polls} poll{'s' if n_polls != 1 else ''}")
    return " · ".join(parts) or "(no artifacts)"


def _sessions_listing(wg: str, cache: str) -> str:
    """Body of `tool_list_sessions` (undecorated) so `read_minutes` can reuse
    it without re-entering the corpus-version pin."""
    mdir = meetings_dir(cache)
    codes = (
        sorted(
            name
            for name in os.listdir(mdir)
            if os.path.isdir(os.path.join(mdir, name)) and not name.startswith("_")
        )
        if os.path.isdir(mdir)
        else []
    )
    if not codes:
        return f"No meeting sessions gathered for {wg}."
    rows = [
        (code, _session_date(cache, code), _session_artifacts(cache, code))
        for code in codes
    ]
    code_w = max(len(c) for c, _, _ in rows)
    date_w = max((len(d) for _, d, _ in rows), default=0)
    lines = [f"{c.ljust(code_w)}  {d.ljust(date_w)}  {a}".rstrip() for c, d, a in rows]
    return (
        f"Gathered meeting sessions for {wg} (code · date · artifacts). "
        f'Read one with `read_minutes(corpus="{wg}", meeting="<code>")`.\n\n'
        + "\n".join(lines)
    )


@_requires_corpus
def tool_list_sessions(wg: str) -> str:
    return _with_freshness(wg, _sessions_listing(wg, _files_dir(wg)))


def _read_polls(cache: str, code: str) -> str:
    """Concatenate a meeting's gathered poll files (small), or '' if none."""
    pdir = polls_dir(cache, code)
    if not os.path.isdir(pdir):
        return ""
    chunks = [
        text
        for name in sorted(os.listdir(pdir))
        if name.endswith(".md")
        and (text := _read_text_capped(os.path.join(pdir, name), 500)).strip()
    ]
    return "\n\n".join(chunks)


@_requires_corpus
def tool_read_minutes(wg: str, meeting: str = "") -> str:
    cache = _files_dir(wg)
    if not meeting:
        listing = _sessions_listing(wg, cache)
        return _with_freshness(
            wg, f"Pass a `meeting` code. Sessions available:\n\n{listing}"
        )
    path = minutes_path(cache, meeting)
    if not os.path.isfile(path):
        return _with_freshness(
            wg,
            f"No minutes gathered for meeting '{meeting}' in {wg}.\n\n"
            + _sessions_listing(wg, cache),
        )
    body = (
        f"# Minutes — {wg} {meeting}\n\n{_read_text_capped(path, _MINUTES_MAX_LINES)}"
    )
    polls = _read_polls(cache, meeting)
    if polls:
        body += (
            "\n\n## Polls\n\n_Raw poll tallies — a poll is a sense of the room, "
            "not a decision; the chair declares consensus._\n\n" + polls
        )
    return _with_freshness(wg, body)


#: Coarse draft lifecycle slugs (Datatracker draft-type states) → friendly
#: labels. This is the whole offline vocabulary — WG-process granularity (WGLC,
#: IESG evaluation) is not persisted and lives only in the live `draft_status`.
_DRAFT_STATE_LABELS = {
    "active": "active I-D",
    "expired": "expired",
    "rfc": "published RFC",
    "repl": "replaced",
    "auth-rm": "withdrawn (author)",
    "ietf-rm": "withdrawn (IETF)",
}


@_requires_corpus
def tool_draft_state(wg: str, state: str = "") -> str:
    manifest = load_documents_manifest(wg)
    if not manifest:
        return _with_freshness(
            wg,
            f"No draft lifecycle state recorded for {wg} — "
            f"{gather_suggestion(wg, purpose='to record it')}.",
        )
    rows = []
    for name in sorted(manifest):
        slug = manifest[name].get("state") or "unknown"
        if state and slug != state:
            continue
        expires = manifest[name].get("expires") or ""
        rows.append((name, _DRAFT_STATE_LABELS.get(slug, slug), expires))
    if not rows:
        present = ", ".join(
            sorted({(rec.get("state") or "unknown") for rec in manifest.values()})
        )
        return _with_freshness(
            wg, f"No drafts in state '{state}' for {wg}. States present: {present}."
        )
    name_w = max(len(n) for n, _, _ in rows)
    label_w = max(len(lab) for _, lab, _ in rows)
    lines = [
        f"{n.ljust(name_w)}  {lab.ljust(label_w)}  {e}".rstrip() for n, lab, e in rows
    ]
    body = (
        f"Draft lifecycle state for {wg} (name · state · expires), offline from "
        "the cache. This is the COARSE lifecycle only — active / expired / "
        "became-RFC / replaced / withdrawn. It does NOT include WG-process state "
        "(WG Last Call, IESG evaluation); for that use the live `draft_status`. "
        "Adoption is derivable from the name (`draft-ietf-<wg>-` is adopted).\n\n"
        + "\n".join(lines)
    )
    return _with_freshness(wg, body)


#: Line caps for the verbatim artifact reads, so one call can't blow the
#: context window; both page via their start_line / read_file_section hint.
_DRAFT_MAX_LINES = 2000
_ISSUE_MAX_LINES = 3000


def _read_file_window(path: str, start_line: int, max_lines: int) -> str:
    """Return a bounded, header-stamped line window of `path`, with a footer
    pointing at how to page further when it is truncated."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return f"Could not read `{os.path.basename(path)}`."
    total = len(lines)
    start = max(1, start_line)
    end = min(total, start - 1 + max(1, max_lines))
    header = f"# {os.path.basename(path)} (lines {start}–{end} of {total})\n\n"
    footer = ""
    if end < total:
        footer = (
            f"\n\n_(showing lines {start}–{end} of {total}; continue with "
            f"`start_line={end + 1}`)_\n"
        )
    return header + "".join(lines[start - 1 : end]) + footer


def tool_get_draft(
    name: str, start_line: int = 1, max_lines: int = _DRAFT_MAX_LINES
) -> str:
    """Verbatim text of the newest cached revision of draft `name`, bounded."""
    path = _find_latest_draft_file(name)
    if path is None:
        return (
            f"No cached draft matching '{name}'. The owning WG must be gathered "
            f"— {gather_suggestion(normalize_draft_name(name), purpose='to fetch it')}, "
            "or call `list_corpora` to see what is available."
        )
    return _read_file_window(path, start_line, min(max_lines, _DRAFT_MAX_LINES))


def _resolve_issue_file(
    cache: str, number: str, repo: str
) -> Tuple[Optional[str], str]:
    """Resolve a per-issue file by number, within `repo` if given else searched
    across every gathered repo. Returns (path, note); path is None with an
    actionable note on a miss or an ambiguous number."""
    if repo:
        path = issue_path(cache, repo, number)
        if os.path.isfile(path):
            return path, ""
        return None, f"No gathered issue #{number} for repo '{repo}' in this corpus."
    directory = issues_dir(cache)
    if not os.path.isdir(directory):
        return None, "This corpus has no gathered GitHub issues."
    matches = [
        (slug, os.path.join(directory, slug, f"{number}.md"))
        for slug in sorted(os.listdir(directory))
        if os.path.isfile(os.path.join(directory, slug, f"{number}.md"))
    ]
    if not matches:
        return None, f"No gathered issue #{number} in any repo of this corpus."
    if len(matches) > 1:
        repos = ", ".join(slug for slug, _ in matches)
        return None, (
            f"Issue #{number} exists in several gathered repos ({repos}); "
            "pass `repo` (owner/repo) to choose one."
        )
    return matches[0][1], ""


@_requires_corpus
def tool_get_issue(wg: str, number: str, repo: str = "") -> str:
    path, note = _resolve_issue_file(_files_dir(wg), str(number), repo)
    if path is None:
        return _with_freshness(wg, note)
    return _with_freshness(wg, _read_file_window(path, 1, _ISSUE_MAX_LINES))


def _render_upcoming_meetings(corpus: str) -> str:
    """The discovery listing: `corpus`'s upcoming numbered + interim meetings,
    each drillable by passing its id back as `meeting`."""
    from . import live_lookup  # pylint: disable=import-outside-toplevel

    meetings, fetched, error = live_lookup.fetch_upcoming_meetings(corpus)
    if error:
        return error
    if not meetings:
        return (
            f"No upcoming meetings are scheduled for `{corpus}` — it may have "
            "nothing on the calendar, or none published yet.\n\n"
            + live_lookup.age_stamp(fetched)
        )
    lines = [f"# {corpus} — {len(meetings)} upcoming meeting(s)\n"]
    for mtg in meetings:
        # One label source (`meeting_label`) for both surfaces; the raw id
        # stays visible on the Logistics line below.
        lines.append(f"- **{mtg.date}** — {live_lookup.meeting_label(mtg.number)}")
        lines.append(f"  - Agenda: {mtg.agenda_url}")
        lines.append(f"  - Logistics: `meeting_sessions({corpus!r}, {mtg.number!r})`")
    lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def tool_meeting_sessions(corpus: str, meeting: str = "") -> str:
    """Render a group's live session logistics at a numbered or interim meeting.

    With no `meeting`, lists the group's upcoming meetings (discovery, since
    an interim id isn't guessable). Lazily imports `live_lookup` (the one
    read-path network module) so the default offline read path never pulls it
    in; registered only behind the gather gate. Times are venue-local,
    converted from the agenda's UTC.
    """
    from . import (  # pylint: disable=import-outside-toplevel
        gather_runner,
        live_lookup,
    )

    corpus = (corpus or "").strip()
    meeting = str(meeting or "").strip()
    if not corpus:
        return "Provide a Working Group shortname (e.g. `httpbis`)."
    if not gather_runner.valid_corpus_name(corpus):
        return f"'{corpus}' is not a valid corpus name."
    if not meeting:
        return _render_upcoming_meetings(corpus)
    if not (meeting.isdigit() or live_lookup.is_interim_number(meeting)):
        return (
            "Provide a numbered IETF meeting (e.g. `126`) or an interim id "
            "(e.g. `interim-2026-aipref-05`), or omit it to list this group's "
            "upcoming meetings."
        )
    return _render_meeting_sessions(corpus, meeting)


def _render_meeting_sessions(corpus: str, meeting: str) -> str:
    """Render one meeting's sessions (numbered or interim), venue-local."""
    from . import live_lookup  # pylint: disable=import-outside-toplevel

    label = live_lookup.meeting_label(meeting)
    sessions, fetched, error = live_lookup.fetch_meeting_sessions(corpus, meeting)
    if error:
        return error
    if not sessions:
        return (
            f"No session for `{corpus}` is scheduled at {label} — the "
            "group may not be meeting, or the agenda may not list it yet.\n\n"
            + live_lookup.age_stamp(fetched)
        )

    lines = [f"# {corpus} at {label} — {len(sessions)} session(s)\n"]
    for idx, sess in enumerate(sessions, start=1):
        lines.append(f"## Session {idx}" if len(sessions) > 1 else "## Session")
        local = f"{sess.start_local}–{sess.end_local} {sess.tz_abbrev}".strip()
        lines.append(f"- **When:** {sess.date}, {local}  ({sess.tz})")
        if sess.start_utc:
            lines.append(f"- **Starts (UTC):** {sess.start_utc}")
        if sess.room:
            lines.append(f"- **Room:** {sess.room}")
        if sess.session_id:
            lines.append(f"- **Session id:** {sess.session_id}")
        if sess.meetecho_full:
            lines.append(f"- **Meetecho (remote):** {sess.meetecho_full}")
        if sess.meetecho_onsite:
            lines.append(f"- **Meetecho (onsite):** {sess.meetecho_onsite}")
        if sess.remote_instructions:
            lines.append(f"- **Remote:** {sess.remote_instructions}")
        if sess.agenda_url:
            lines.append(f"- **Agenda:** {sess.agenda_url}")
        if sess.minutes_url:
            lines.append(f"- **Minutes:** {sess.minutes_url}")
        lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


#: Human label per agenda-eligibility signal (`live_lookup.DraftStatus`).
_ELIGIBILITY_LABELS = {
    "in-wg": "in the WG — agenda-eligible",
    "in-iesg": "past the WG (IESG processing)",
    "published": "published as an RFC",
    "dead": "expired or replaced",
    "unknown": "unknown",
}


def tool_draft_status(name: str) -> str:
    """Render one draft's live Datatracker status + agenda-eligibility signal.

    Live read-path tool (lazy `live_lookup` import, gather-gated). Reports the
    draft state, the resolved IESG state, expiry, RFC number, and the derived
    in-wg / in-iesg / published / dead signal an agenda decision turns on.
    """
    from . import live_lookup  # pylint: disable=import-outside-toplevel

    name = (name or "").strip()
    if not name:
        return (
            "Provide a draft name, e.g. `draft-ietf-httpbis-resumable-upload` "
            "(the version suffix is optional)."
        )

    status, fetched = live_lookup.fetch_draft_status(name)
    if status is None:
        canonical = normalize_draft_name(name)
        return (
            f"Datatracker has no document named `{canonical}`. Check the "
            "`draft-...` stem (version optional).\n\n" + live_lookup.age_stamp(fetched)
        )

    label = _ELIGIBILITY_LABELS.get(status.eligibility, status.eligibility)
    lines = [f"# {status.name}\n"]
    if status.rev:
        lines.append(f"- **Revision:** -{status.rev}")
    if status.draft_state:
        lines.append(f"- **Draft state:** {status.draft_state}")
    if status.iesg_state:
        lines.append(f"- **IESG state:** {status.iesg_state}")
    if status.rfc_number:
        lines.append(f"- **RFC:** {status.rfc_number}")
    if status.intended_status:
        lines.append(f"- **Intended status:** {status.intended_status}")
    if status.expires:
        lines.append(f"- **Expires:** {status.expires[:10]}")
    lines.append(f"- **Agenda eligibility:** {label}")
    if status.note:
        lines.append(f"\n> {status.note}")
    lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


@graceful_keyboard_interrupt
def main() -> None:  # pylint: disable=too-many-locals
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

    # Resolve the in-session gather default up front (see
    # `_startup_gather_default`: stdio on, http off, immutable mount off). It
    # must be established *before* the gather tools' registration gate below
    # so the gate and the user-facing "go gather" hints read the same resolved
    # value; IETF_LLM_ENABLE_GATHER still overrides either way.
    transport = _resolve_transport()
    set_gather_default(_startup_gather_default())

    # `instructions` is the MCP-spec mechanism for server-level
    # guidance: clients SHOULD surface it as system-prompt context.
    # Loading SKILL.md here makes the same guidance Claude Code reads
    # from the installed skill available to Codex / Gemini / Cursor /
    # Zed / opencode — one source of truth, no parallel maintenance.
    server_instructions = _load_server_instructions()
    # Append the version/feature footer so a skill can feature-gate from the
    # prompt; harmless to start without SKILL.md (footer becomes the whole
    # instructions string).
    server_instructions = (server_instructions or "") + _capability_footer()
    # HTTP transport knobs (ignored by stdio): stateless sessions (default on,
    # so any replica answers any request) and an optional Host/Origin allow-list
    # for DNS-rebinding protection when the server is fronted directly (#41).
    server = FastMCP(
        "ietf-llm",
        instructions=server_instructions,
        stateless_http=_stateless_http_enabled(),
        transport_security=_transport_security_settings(),
    )
    # Report ietf-llm's own version in the `initialize` handshake's
    # `serverInfo.version` (what `claude mcp`, Cursor, etc. display). FastMCP's
    # constructor takes no `version`, so the lowlevel server otherwise falls
    # back to reporting the `mcp` SDK version. Same private-but-stable
    # `_mcp_server` attribute the stdio transport already drives below.
    server._mcp_server.version = __version__  # pylint: disable=protected-access

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
        mailing list it follows, or the author it tracks — and a trailing
        `(…)` **source inventory** (which of mailing `list`, GitHub
        `issues`, `drafts`, `RFCs`, `minutes` are present) — so you can see
        what a corpus covers, and whether GitHub issues were gathered at
        all, without opening it. Call `overview` for the gather window and
        the exact repos.
        """
        return await _offload(tool_list_corpora)

    @server.tool()
    async def find_efforts(query: str, limit: int = 15) -> str:
        """Find active IETF/IRTF efforts by **topic** — the entry point
        for "what is the IETF doing around X?" when no working group is
        named. Returns a ranked markdown list of working/research groups,
        each tagged with whether it is **already gathered here** (`✓
        cached`); prefer those.

        Each row carries the effort's Datatracker **state**: a `bof` row,
        shown as **BoF — pre-WG, not chartered**, is *not* a Working Group —
        don't read it (or a stray agenda / draft) as one.

        This is the topic→effort discovery step the corpus-first tools
        lack. Reach here when the user gives a *subject* with no obvious
        home — "AI", "post-quantum", "congestion control", "email
        security" — instead of guessing a corpus or crawling Datatracker /
        the web. It ranks over the official Datatracker group list
        (acronym + name + charter description), mirrored locally; it covers
        **active** and **BoF** groups only, so a concluded effort or
        published work won't surface here — use `rfc_search` for the RFC
        series, and `list_corpora` to see what is already cached.

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
    async def which_corpus(query: str, limit: int = 8) -> str:
        """Route a question to the **already-gathered** corpus it belongs to,
        when the user gives a topic but names no working group. Embeds the
        question and ranks gathered corpora by similarity to their topic-map
        centroids; returns the ranked names with scores, or **abstains** when
        nothing is close.

        This is the "which corpus did they mean" step. It is distinct from
        `find_efforts`, and the two answer different questions:
          - `which_corpus` ranks what is **already cached here**, by your
            actual gathered content — use it to pick the corpus for a question
            like "where is 0-RTT replay discussed?" without naming one.
          - `find_efforts` ranks the **Datatracker catalog** (mostly
            *un-gathered* efforts) to decide what to gather next.
        When unsure which to reach for: have a question about a topic that is
        probably already gathered → `which_corpus`; exploring what the IETF is
        doing about a subject you may not have gathered → `find_efforts`.

        It is a **router, not a searcher**: it does not read content. Follow a
        confident result with `search_corpus(corpus, query, ...)` (or
        `search_corpora` to compare a few) — the score is topic proximity, not
        proof the answer is there. When it abstains (top score below the
        confidence floor), fall back to `find_efforts` rather than forcing a
        search against a low-confidence guess. Corpora gathered before the
        topic map shipped have no centroids and are reported as such until
        re-gathered. `limit` caps results (default 8).
        """
        return await _offload(tool_which_corpus, query, limit)

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
    async def overview(corpus: str, live: bool = False) -> str:
        """**Prefer this to web search to orient on an IETF/IRTF effort** — a
        working group, research group, BoF, mailing list, or draft set — in
        one call: chairs/ADs, active drafts, main discussion themes, top open
        issues, recent mailing list threads, latest meeting and latest draft
        publication.

        The **main discussion themes** are topical clusters of the gathered
        record (computed at gather time from the embedding index); each
        names what the group keeps coming back to, with how much traffic and
        when it was last active. Themes that recur across many gathered
        corpora (meeting logistics, ballots) are demoted below the distinctive
        ones and tagged _common across WGs_. Search a theme's wording with
        `search_corpus`. (Absent on a corpus gathered before this shipped —
        re-gather to populate.)

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
            `read_digest(corpus, kind="issues", label="...",
            include_bodies=True)` — the issue catalogue plus each
            opening description in one call beats semantic search for
            coverage (`list_labels` first if you don't know the labels).
          - "what did the WG decide about X?" / "what's the WG's
            position on X?" → the outcome is whatever the chairs
            declared, so go to their words: `search_corpus(corpus, "X",
            role="Chair")` and `tally_positions(corpus, "<thread or
            issue file>")`. This corpus does not compute consensus.
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

        Ends with a **## Coverage** section: which sources the corpus
        holds (mailing list, GitHub issues — by repo — drafts, RFCs,
        minutes) and, in the leading `Coverage:` line, how far back the
        windowed sources reach. The window bounds *mailing-list and
        meeting* recency only (default 12 months); issues and drafts are
        the full set. If the user asks about list/meeting activity older
        than the window, re-gather deeper rather than reporting nothing.

        Other ietf-llm tools: `read_digest`, `search_corpus`,
        `read_topic`, `get_chunk_text`, `read_file_section`,
        `list_files`, `list_labels`.

        **Collective-outcome claims are gated:** before asserting that
        something is settled / decided / agreed / rejected, that there is
        consensus, or what "the WG wants" (vs reporting what a *named
        individual* said, which is free), you must have called
        `read_ietf_interpretation_norms` this session — see it for the full
        rule. Write side (drafting a contribution):
        `read_ietf_participation_norms`.

        `live=True` appends a **## Live draft reconciliation** section that
        cross-checks the (cache-derived) active-draft list against Datatracker
        and flags divergence — a draft listed active here that has actually
        advanced past the WG (drop from an agenda), or an adopted draft active
        on Datatracker that the cached list omits (a revived draft to re-gather
        and consider). Use it when building an agenda or whenever the
        active-draft list must be exactly right; it hits Datatracker live (so
        it is only available where gather is enabled — local stdio, off on the
        shared HTTP replica) and is slower than the default offline overview.

        Args:
            corpus: The corpus shortname (`httpbis`, `tls`, …).
            live: Reconcile the active-draft list against live Datatracker.
        """
        return await _offload(tool_overview, corpus, live)

    @server.tool()
    async def read_ietf_interpretation_norms() -> str:
        """Return the interpretive norms for reading an IETF corpus:
        how consensus works (chair-declared, not vote-counted), how
        to attribute positions (individuals, not employers), and
        why mailing-list confirmation — not meeting agreement —
        is the binding decision.

        **Call this before writing any sentence that asserts a collective
        outcome** — that something is settled, decided, resolved, agreed,
        or rejected, that there is consensus, or what "the WG thinks/wants".
        The trigger is grammatical, not a self-assessment: reporting what a
        named individual said is free; any claim about where the *group*
        landed is gated, however confident you are. Not needed for catalogue
        lookups (`read_digest`), text fetches (`read_file_section`), or
        structural questions (`overview`). The content is stable across
        corpora — one call per session is enough. For the write side
        (drafting a contribution), see `read_ietf_participation_norms`.
        """
        return await _offload(tool_read_interpretation_norms)

    @server.tool()
    async def read_ietf_participation_norms() -> str:
        """**Mandatory before drafting any contribution** — before you
        write a single line of list mail, a reply in a thread, a GitHub
        issue or comment, a review, or a consensus/position statement:
        any text that will go into the record under a person's name. Read
        this FIRST, not as an afterthought; reading the *interpretation*
        norms does not substitute. The moment a task turns from reading the
        corpus to producing a contribution — "write/draft an email to the
        working group", "reply to this thread", "respond on the list",
        "file/comment on an issue", "compose list mail" — call this.

        Covers: the human is accountable and sends (you only draft),
        disclosing AI involvement and how closely supervised, the register
        to match (terse, technical, no AI tells), staying on-charter,
        engaging existing work rather than dropping new ideas cold, not
        re-litigating settled questions or manufacturing consensus signal,
        and where AI help is uncontroversial (summarise/translate, explain
        ABNF/YANG). Authoring Internet-Drafts is out of scope. Stable
        across corpora — one call per session is enough. For
        reading/characterising a corpus, see
        `read_ietf_interpretation_norms`.
        """
        return await _offload(tool_read_participation_norms)

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
    async def find_message_citations(
        corpus: str, file: str, chunk_idx: Optional[int] = None
    ) -> str:
        """Walk the message reference graph for a thread / issue file —
        which messages cite it, and which archive links it cites.

        Messages cite *other messages* by archive permalink constantly:
        an appeal links the post being appealed, a split thread links the
        message it forked from, a reply footnotes the one it answers. The
        gather step resolves those links into `digests/message_citations.md`;
        this reads the graph for one file.

        Returns **Inbound** (other messages that cite this file — the
        reverse index `fetch_by_url` can't give you) and **Outbound** (the
        archive links this file cites, each resolved to a local
        `file` / `chunk_idx` to pivot on, or flagged external — a message
        not gathered here, often another list to gather and retry).

        Use when:
          - Reading a message whose body footnotes an archive URL and you
            want the message behind it (or `fetch_by_url` for one URL).
          - Tracing a dispute / appeal / split thread back to its origin.
          - Asking "who else referenced this message or decision?".

        `file` is corpus-relative (`threads/<file>.md`, `issues/<repo>/<n>.md`);
        pass `chunk_idx` to scope to a single message. Within-scheme
        resolution only (a `mailarchive` token is not bridged to a
        `w3.org/mid` Message-ID), so real targets can show as external on
        a list that stamps the opposite scheme from what bodies cite.
        """
        return await _offload(tool_find_message_citations, corpus, file, chunk_idx)

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
    async def draft_authors(name: str) -> str:
        """The authors/editors of a draft, with contact emails — for a
        call-for-presenters or to reach a draft's owners.

        Reads the Authors' Addresses section of the newest **cached** revision
        of the draft (across all gathered corpora); offline, no network. Each
        entry gives the name, role (author/editor), organisation, and the
        email the draft itself lists. The owning corpus (the draft's WG) must
        be gathered. These are draft-stated addresses — a chair may have a
        better working address from mail and can override.

        Args:
            name: The draft name (`draft-ietf-httpbis-resumable-upload`); the
                version suffix is optional (the newest cached revision is used).
        """
        return await _offload(tool_draft_authors, name)

    @server.tool()
    async def list_sessions(corpus: str) -> str:
        """List a corpus's gathered meeting sessions — each meeting code with
        its date and which artifacts are present (minutes, agenda, transcripts,
        polls). Offline, from the cache. Use it to find the `meeting` code to
        pass to `read_minutes`, or to see which sessions were captured.
        """
        return await _offload(tool_list_sessions, corpus)

    @server.tool()
    async def read_minutes(corpus: str, meeting: str = "") -> str:
        """Read the gathered minutes for one meeting session, plus any recorded
        poll tallies. Offline, from the cache; the authoritative record of what
        a session discussed and decided.

        Pass the `meeting` code from `list_sessions` (e.g. `ietf125`,
        `interim20260401`); omit it to get the session list. The appended polls
        are raw sense-of-the-room tallies, NOT decisions — the chair declares
        consensus (see `read_ietf_interpretation_norms`).
        """
        return await _offload(tool_read_minutes, corpus, meeting)

    @server.tool()
    async def draft_state(corpus: str, state: str = "") -> str:
        """Draft lifecycle state for a corpus, offline from the cache: which
        drafts are active, expired, became RFCs, were replaced, or withdrawn,
        with expiry dates. Optionally filter to one `state` slug.

        COARSE lifecycle only — it does NOT include WG-process state (WG Last
        Call, IESG evaluation); for that use `draft_status` (live). Adoption is
        derivable from the draft name (`draft-ietf-<wg>-` is adopted). The
        offline counterpart to `draft_status` for when the network / live path
        is unavailable.
        """
        return await _offload(tool_draft_state, corpus, state)

    @server.tool()
    async def get_draft(name: str, start_line: int = 1, max_lines: int = 2000) -> str:
        """Verbatim text of a cached Internet-Draft by name (newest cached
        revision, across all gathered corpora), as a bounded line window.

        Use this to quote a draft's ACTUAL wording — to ground a review, a
        citation, or a contribution in primary text rather than a search
        snippet. Page a long draft with `start_line`. The owning WG must be
        gathered.
        """
        return await _offload(tool_get_draft, name, start_line, max_lines)

    @server.tool()
    async def get_issue(corpus: str, number: str, repo: str = "") -> str:
        """Verbatim text of one GitHub issue — opening description and comment
        thread — from a corpus, by issue number.

        Use this to quote an issue's ACTUAL text for a citation rather than a
        search snippet. Pass `repo` (owner/repo) to disambiguate when the
        corpus tracks several repos and the number is ambiguous.
        """
        return await _offload(tool_get_issue, corpus, number, repo)

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
        diversify: bool = True,
    ) -> str:
        """Search the gathered record of an IETF/IRTF effort — a working
        group, research group, mailing list, or set of Internet-Drafts —
        semantically across its mailing-list debate, GitHub issues,
        drafts, slides, transcripts, and minutes. (Published RFC bodies
        aren't indexed here by default — search the series with
        `rfc_search` and read one with `get_rfc`.) Returns top-k
        chunks with file, chunk_idx, title, score, snippet, line range,
        GitHub URL (for issue chunks), and (for issue chunks) the issue's
        GitHub labels + open/closed state.

        **Prefer this to web search** for any question about what an
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

        `diversify=True` (the default) spreads the results across the
        threads/issues that match instead of returning five chunks of
        the one most-relevant thread — better for "what are the angles
        on X?". Set False for the raw relevance ranking when you want
        every closely-matching chunk even if they overlap. Has no effect
        under `sort="date"` (a timeline keeps adjacent messages) or
        `group_by="file"` (already one row per file).

        Requires the embedding index (built by default on gather;
        skipped only with `--no-embed`).

        Optional facets:
          - file_pattern: SQL LIKE pattern over the relative path
            (e.g. "threads/%" to restrict to mailing-list threads,
            "issues/%" for GitHub issues, "drafts/%" for drafts).
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
            diversify=diversify,
        )

    @server.tool()
    async def find_related(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        file: str,
        chunk_idx: int,
        k: int = 10,
        file_pattern: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        label: Optional[str] = None,
        state: Optional[str] = None,
        group_by: Optional[str] = None,
        snippet_chars: Optional[int] = None,
        diversify: bool = True,
        collapse_versions: bool = True,
    ) -> str:
        """Find the chunks most similar to one you already have — a
        nearest-neighbour-by-example search over the same index
        `search_corpus` uses. Where `search_corpus` takes a query
        *string*, this takes an existing chunk (`file` + `chunk_idx`, the
        identity in every search hit and `get_chunk_text` call) and
        returns the others closest to it in meaning. The seed chunk is
        excluded from its own results.

        Reach for this after a search or a read when you want "more like
        this": other threads making the same argument, prior issues on
        the same point, the drafts a message is really about — without
        having to guess the right query words.

        **Cross-surface bridging** is the highest-value use. A topic is
        usually discussed in BOTH the mailing list and a GitHub issue;
        they sit close together in the index but aren't linked. Seed on a
        thread message and pass `file_pattern="issues/%"` to surface the
        issue(s) that capture it (add `group_by="file"` for one row per
        issue) — or seed on an issue comment with `file_pattern="threads/%"`
        for the list discussion behind it.

        Facets (`file_pattern`, `since`/`until`, `label`, `state`,
        `group_by`, `snippet_chars`, `diversify`, `collapse_versions`)
        behave as in `search_corpus`. `collapse_versions=True` (the
        default) matters when the seed is near a draft: it hides older
        revisions of a draft when a newer one also matched, so a query
        doesn't return `-01`/`-02`/`-03` of the same draft as separate
        hits. Unlike `search_corpus` this needs no query embedding — it
        reads the seed's stored vector — so it answers even when the
        embedding backend is unavailable.

        `chunk_idx` is the 0-based index shown in search hits
        (`chunk=N`). Use `list_files` to see how many chunks a file has.
        """
        return await _offload(
            tool_find_related,
            corpus,
            file,
            chunk_idx,
            k=k,
            file_pattern=file_pattern,
            since=since,
            until=until,
            label=label,
            state=state,
            group_by=group_by,
            snippet_chars=snippet_chars,
            diversify=diversify,
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
        """Semantic search across **several** gathered corpora in one
        call, returning merged, rank-ordered hits each tagged with the
        `corpus=` they came from — the cross-corpus companion to
        `search_corpus`, fanned over the set you name. **Prefer this to
        web search** — it reads the groups' primary record, not
        second-hand coverage.

        This is **breadth, not depth**: it locates *where* a cross-cutting
        topic ("what is the IETF doing around AI?") lives across efforts;
        pivot to the single-corpus tools (`read_topic`, `tally_positions`,
        `read_digest`, `search_corpus`) for the decisions and narrative.
        Assemble `corpora` from `find_efforts` — the few efforts that
        dominate the topic, not a blind scan — then query them here in one
        call.

        `corpora` is **required**: unknown names, corpora with no embedding
        index, and any past the 12-corpus cap are skipped and reported, not
        silently dropped.

        **Score comparability.** Cosine scores compare directly only across
        corpora built with the **same embedding model**. One shared model →
        a single ranked list. Mixed models → grouped by model, ranked
        within each, groups interleaved by rank (the header says which were
        grouped).

        `k` bounds the **total** merged hits (default 10). Facets mirror
        `search_corpus` per corpus: `since`/`until`, `label`, `state`,
        `author`, `role`, `snippet_chars`, `collapse_versions`. The
        depth-only knobs (`sort`, `group_by`, `file_pattern`) are omitted —
        scope a single corpus for those. Read-only; requires each corpus's
        embedding index.
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
        """Surface the procedural backbone of ONE mailing-list thread or
        GitHub issue of an IETF/IRTF effort. Its high-value output is the
        **Chair statements** section at the top: any message from a chair
        containing procedural language (`rough consensus`, `consensus
        call`, `WGLC`, `adopting`, `closing this thread`, …) rendered
        prominently with an excerpt — that is where a group's *decision*
        actually lives, because IETF consensus is **chair-declared**.

        Below that is a per-author count of canonical position *phrasings*
        (`+1`, `-1`, `I support`, `I object`, `LGTM`, conditional support,
        `DISCUSS`). Read this as a **rough keyword heuristic, NOT a measure
        of consensus or level of support** — it matches surface phrasings,
        not sentiment, and the IETF does not decide by counting. Never quote
        the count as "the WG supported X by N to M". Use it only to *locate*
        who said something explicit, then read their actual message.

        Coverage percentage tells you what fraction of messages the
        heuristic could classify at all — low coverage means the count is
        nearly meaningless. To characterise an outcome, go to the chair's
        declared words (this tool's Chair statements, plus
        `search_corpus(role="Chair", state="closed")`), and call
        `read_ietf_interpretation_norms` first.

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

        **This is *narrative* — what individuals said, never an outcome.**
        Before asserting any *collective* outcome from it (settled / decided
        / agreed / consensus / "the WG wants"), you must have called
        `read_ietf_interpretation_norms` this session — see it for the full
        rule.

        **Prefer this to web search** when the user wants the *arc* of how
        a working group / research group discussion on a topic evolved —
        it reconstructs the real conversation from the gathered list and
        issue traffic, not the web's recap. The unit is a message, not a
        chunk: each matched thread message or issue comment appears in
        full, so you get "who said what when" without N follow-up
        `get_chunk_text` calls.

        `body_chars` caps each message body (default 4000; min 100). Dial
        it down for a synthesis task where the gist of each message is
        enough — the slice costs far less context, and a truncated body
        still points at `get_chunk_text` for the full text.

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
        ietf-llm cache (per-thread files, per-issue files, drafts,
        slides, transcripts, minutes; RFC bodies only when gathered with
        `--rfcs` — otherwise use `get_rfc`). Default 400 lines per call; the
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
    # contract — so they are registered only when gather is enabled. That
    # defaults on for local stdio (the user can already run `ietf-llm`
    # against the same cache) and off for the shared HTTP replica (keeping it
    # read-only); IETF_LLM_ENABLE_GATHER overrides either way. The default is
    # resolved from the transport at the top of `main`, before this gate.
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
            wait: Optional[float] = None,
        ) -> str:
            """Gather a new corpus into the local cache.

            Use this when a corpus the user asks about isn't cached yet
            (`list_corpora` doesn't show it). **By default this blocks** until
            the gather finishes (up to ~90s) and reports `done`, so the common
            gather-then-read flow is a single call — no poll loop, no guessed
            sleep. A quick re-gather usually completes within that window; a
            *first* gather of a corpus can run for minutes, so if it is still
            going when the wait elapses the reply falls back to a progress line
            plus a stop token — then poll `gather_status(corpus=...)` until it
            reports `done`. Pass `wait=0` to return immediately instead
            (fire-and-forget: kick off the gather and do other work), or a
            custom `wait` (seconds) to block longer or shorter. Later
            re-gathers fetch only what changed since last time.

            The corpus **shape is inferred** from what you pass — you don't
            declare it:
            - **Working Group / RG / BoF**: pass just `corpus` as the
              shortname (`tls`, `cfrg`). The charter, drafts, meetings,
              mailing list, and GitHub issues are auto-discovered (the WG's
              published RFCs are listed in the overview but their bodies stay
              in the global series — `rfc_search` / `get_rfc`)
              — including *which* repos to track: the first gather finds the
              group's active draft repos and follows them automatically
              (`gather_status` reports which were added). To preview or
              override that choice, call `suggest_github_repos` and pass the
              result as `github`. Repo discovery calls the GitHub API, so the
              server should have `GITHUB_TOKEN` set; without it a busy host
              can be rate-limited and track no repos (the status notes say so,
              and it retries next gather).
            - **Standalone mailing list**: pass `corpus` as the list name
              (`last-call`); auto-detected when it isn't a known group.
            - **Custom set**: any label as `corpus` plus explicit
              `mailing_list` / `draft` / `github` sources.
            - **Follow an author / new drafts**: `author` (email, person
              id, or exact name) or `new_drafts=True` (rolling window).
            - **Synthetic**: an `x-` `corpus` name with explicit sources.

            One gather per corpus runs at a time, across all hosts on a
            shared deployment — so a call while it's in flight reports
            "already running" even if another client started it. A
            *different* corpus runs concurrently up to a small cap; beyond
            that it reports "queued". Either way, poll `gather_status`,
            don't retry or `force` (which overrides only the freshness
            debounce below, never these limits).

            A corpus gathered within the freshness window (default 6h) is
            **not** re-gathered — the call returns a "fresh, skipped" note.
            That is success: query the existing snapshot, don't retry. Only
            pass `force=True` when the user explicitly wants fresh data.

            Custom / synthetic (`x-`) names are free-form and don't self-
            deduplicate, so before minting one this checks `list_corpora` for
            an existing corpus over the same sources. On overlap it returns a
            reuse hint instead of gathering — prefer the existing corpus;
            `force=True` mints the near-duplicate anyway.

            LLM summarisation (one-line digests of threads / issues) is
            deliberately **not** an option here: it needs an LLM key and is the
            slowest gather step, and an MCP-initiated gather stays lean. It is
            CLI/env-only — set `IETF_LLM_SUMMARIZE` (and optionally
            `IETF_LLM_SUMMARIZE_MODEL`) on the gather host, or run
            `ietf-llm <corpus> --summarize`.

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
                months: Months of mailing-list / meeting history to fetch
                    (default 12). 0 means all history — an unbounded, slow
                    gather, so it is refused unless `force=True`.
                add_mentioned_drafts: Also pull in drafts the corpus cites
                    but doesn't already have.
                include_related_drafts: Also gather related (un-adopted)
                    drafts the WG follows. Can be large.
                github_label: Include only issues with these labels.
                exclude_github_label: Exclude issues with these labels.
                force: Re-gather even if the corpus is within the freshness
                    window (and mint a near-duplicate custom corpus despite an
                    overlap hint). Overrides the freshness debounce only — it
                    never starts a second gather while one is running. Use only
                    on an explicit request for fresh data.
                wait: Seconds to block waiting for the gather to finish before
                    returning a progress line to poll on. Omit to block for the
                    default (~90s); `0` returns immediately (fire-and-forget).
                    Clamped to stay under the server's per-call tool deadline.
                    Also waits when the corpus is already being gathered (by
                    another client or a CLI run).
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
                wait,
            )

        @server.tool()
        async def gather_status(
            corpus: Optional[str] = None, wait: Optional[float] = None
        ) -> str:
            """Report the progress of background gathers started with
            `start_gather`.

            With `corpus`, returns that corpus's state: `running` (with the
            current stage, e.g. `stage 7/17 (github issues)`, and elapsed
            time), `done`, `failed` (with the error), or `interrupted` (the
            server process ended mid-gather — re-run `start_gather`). With no
            argument, lists every recorded gather, most-recently-active
            first. Once a corpus reports `done`, the read tools (`overview`,
            `search_corpus`, …) work on it.

            Returns the current state immediately by default. Pass `wait`
            (seconds) to **block** until a still-running gather reaches a
            terminal state (or the wait elapses) — the no-sleep way to wait out
            the tail of a long first gather after `start_gather`'s own wait
            returned it still in progress. Clamped under the server's tool
            deadline; ignored for the no-`corpus` list-all form.

            Don't query before `done`. The catalogue and search layers
            (digests, embedding index) are built in the *final* gather
            stages, so a mid-gather corpus has at most raw `threads/` /
            `issues/` / `drafts/` files — `overview`, `read_digest`, and
            `search_corpus` are empty or partial until the end. (On the
            cloud backend nothing is visible at all before the final
            atomic publish.)

            Args:
                corpus: The corpus to report on. Omit to list all.
                wait: Seconds to block for a still-running gather to finish
                    before reporting. Omit/`0` reports immediately. Clamped
                    under the server's per-call tool deadline.
            """
            return await _offload(tool_gather_status, corpus, wait)

        @server.tool()
        async def stop_gather(corpus: str, token: str) -> str:
            """Stop an in-flight gather started with `start_gather`.

            Cooperative and best-effort: the gather ends at its next stage
            boundary or download batch, so this reports that a stop was
            *requested* — poll `gather_status(corpus=...)` until it reports
            `cancelled`. The partial download is discarded (not published), so a
            previously-gathered snapshot of the corpus is left intact.

            Requires the `token` returned by the `start_gather` call that began
            this gather: it is the only capability that can stop it, so one
            client cannot cancel another client's gather. A wrong or missing
            token is refused, as is a corpus with no gather in flight.

            Use this when a gather is taking too long or was started by mistake
            (e.g. an over-wide `months` window on a busy list) and you want to
            free the slot rather than wait it out.

            Args:
                corpus: The corpus whose gather to stop.
                token: The stop token returned by `start_gather`.
            """
            return await _offload(tool_stop_gather, corpus, token)

        @server.tool()
        async def suggest_github_repos(corpus: str) -> str:
            """Discover which GitHub repos a Working Group's gather should
            track, before calling `start_gather`.

            A WG's Datatracker record points at a GitHub org that usually holds
            many repos — only some are where drafts are actually discussed.
            This reads that org, finds the repos that both carry Internet-Draft
            sources and have an active issue tracker, and returns a ranked list
            plus the exact `github=[...]` to pass to `start_gather`.

            You don't *need* to call this first: a group-backed `start_gather`
            with no `github` already auto-tracks the high-confidence repos on
            the corpus's first gather. Use this to preview or override that
            choice, to pick up 'maybe' repos it didn't auto-track, or for a
            corpus that has already been gathered once.

            Hits the GitHub API, so the server should have `GITHUB_TOKEN` set
            (strongly encouraged); without it the call can be rate-limited and
            the result will say so and may be incomplete.

            Args:
                corpus: The Working Group shortname (e.g. `tls`, `httpbis`).
            """
            return await _offload(tool_suggest_github_repos, corpus)

        @server.tool()
        async def meeting_sessions(corpus: str, meeting: str = "") -> str:
            """A group's session logistics at an IETF meeting, **live** from
            Datatracker — useful to any attendee or observer (building an
            agenda is the obvious case).

            Handles both **numbered** meetings (e.g. `126`) and **interim**
            meetings (e.g. `interim-2026-aipref-05`). Returns every session the
            group has at that meeting (a WG can have two), each with the
            venue-**local** weekday/date and start–end time (converted from the
            agenda's UTC, DST-correct), the room, the Datatracker session id,
            and the agenda/minutes links. Numbered meetings add both Meetecho
            URLs (remote + onsite); interims have no onsite room, so they carry
            the agenda's free-text remote instructions (a Meetecho URL, a Teams
            note, …) instead.

            **Omit `meeting`** to list the group's upcoming meetings (numbered +
            interim) — the way to discover an interim id, which isn't guessable.

            Live (short TTL + freshness stamp), gather-gated — off on the shared
            HTTP replica; see the SKILL "Live Datatracker facts" section for why.
            Times are venue-local — never quote the UTC start as the local time.

            Args:
                corpus: The Working Group shortname (e.g. `httpbis`).
                meeting: A numbered meeting (`126`) or interim id
                    (`interim-2026-aipref-05`); omit to list upcoming meetings.
            """
            return await _offload(tool_meeting_sessions, corpus, meeting)

        @server.tool()
        async def draft_status(name: str) -> str:
            """**First call for where an IETF draft actually stands** — prefer
            it to web search for any "what state is draft-… in / how far along is
            it / is it in WGLC, IESG, or published" question. One draft's current
            status, **live** from Datatracker, with a derived eligibility signal.

            Returns the revision, the draft state (Active / Expired / Replaced /
            RFC), the IESG state (`I-D Exists`, `AD Evaluation`, `IESG
            Evaluation`, `RFC Ed Queue`, …), the expiry date, the intended
            status, and the RFC number if published — plus a derived signal:
            **in-wg** (still in WG hands), **in-iesg** (past the WG, in IESG
            processing), **published**, or **dead** (expired or replaced). The
            gather cache's curated active-draft list can lag the real IESG state
            by days, so reach here when the *current* standing matters (deciding
            an agenda is the obvious case).

            Live (short TTL + freshness stamp), gather-gated — off on the shared
            HTTP replica; see the SKILL "Live Datatracker facts" section.

            Args:
                name: The draft name (`draft-ietf-httpbis-resumable-upload`);
                    the version suffix is optional.
            """
            return await _offload(tool_draft_status, name)

    _prewarm_embedding_model_async()
    if transport == "http":
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


def _startup_gather_default() -> bool:
    """The in-session gather default resolved at startup, before the
    registration gate.

    On for a local stdio server (that user can already run `ietf-llm`
    against the same cache), off for the shared HTTP deployment (which stays
    read-only), and off regardless when `IETF_LLM_INDEX_IMMUTABLE` marks the
    mount read-only — a read-only mount must not *default* a writer on (the
    HTTP path already hard-refuses the explicit `ENABLE_GATHER` + immutable
    combination; this keeps the stdio default from silently picking a writer
    that would fail at index-write time). `IETF_LLM_ENABLE_GATHER` is still
    an explicit override on top of this default, in either direction.
    """
    return _resolve_transport() == "stdio" and not _index_immutable_enabled()


#: Accepted IETF_LLM_LOG_LEVEL spellings -> the serve verbosity they select.
#: Numeric aliases match the Verbosity enum values; `progress` is a convenience
#: spelling for the most verbose level (it is the level the chattiest log calls
#: carry).
_LOG_LEVEL_NAMES: "Dict[str, Verbosity]" = {
    "quiet": Verbosity.QUIET,
    "0": Verbosity.QUIET,
    "status": Verbosity.STATUS,
    "1": Verbosity.STATUS,
    "verbose": Verbosity.VERBOSE,
    "progress": Verbosity.VERBOSE,
    "2": Verbosity.VERBOSE,
}


def _log_verbosity() -> Verbosity:
    """Resolve the verbosity the serve path logs at.

    `IETF_LLM_LOG_LEVEL` (quiet / status / verbose) is the explicit override.
    When it is unset the default is transport-aware: the HTTP serve path
    defaults to STATUS so a hosted container emits per-request access records
    and notable events on the structured stream out of the box (the platform
    captures stderr and there is no other operational signal there), while
    stdio — the local CLI / desktop client — defaults to QUIET so an
    interactive session stays silent. An unrecognised value falls back to that
    same transport default rather than guessing. Cheap enough to call per
    request (a couple of env reads)."""
    raw = os.environ.get("IETF_LLM_LOG_LEVEL", "").strip().lower()
    if raw in _LOG_LEVEL_NAMES:
        return _LOG_LEVEL_NAMES[raw]
    return Verbosity.STATUS if _resolve_transport() == "http" else Verbosity.QUIET


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
        # In-session gathers running now. A stable JSON field so a fronting
        # proxy can decide whether to keep an idle-timing-out container alive
        # past its window (a background gather publishes nothing until it
        # finishes) without scraping/parsing the `/metrics` text format.
        "gathers_inflight": serve_metrics.gathers_inflight(),
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

    body = serve_metrics.render(_corpus_ages(), version=__version__)
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


def _csv_env(name: str) -> "List[str]":
    """A comma-separated env var as a list of stripped, non-empty items."""
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


def _stateless_http_enabled() -> bool:
    """Whether the HTTP transport runs stateless (no per-client session).

    Default **on**: a stateless server keeps no `Mcp-Session-Id` state between
    requests, so any replica behind a load balancer can answer any request with
    no session affinity — the right shape for this read-mostly server, and what
    a horizontally-scaled deployment wants. Set `IETF_LLM_MCP_STATELESS=0`
    (or false/no/off) to restore stateful sessions. stdio ignores this.
    """
    raw = os.environ.get("IETF_LLM_MCP_STATELESS", "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _transport_security_settings() -> "Any":
    """DNS-rebinding (Host/Origin) protection settings for the HTTP transport.

    Off by default: the server assumes a trust boundary (proxy / firewall) in
    front and does no Host validation itself (#41). This is an **explicit**
    disable, not an omission — the MCP library otherwise defaults to a
    loopback-only allow-list (`127.0.0.1:*` / `localhost:*` / `[::1]:*`), which
    silently answers a fronted public-hostname deployment with `421 Invalid Host
    header`. Returning a settings object with protection off restores the
    documented "bind wide behind a proxy" shape.

    When `IETF_LLM_MCP_ALLOWED_HOSTS` is set, enable validation and accept only
    those `Host` values — each an exact `host` / `host:port`, or a `host:*`
    wildcard that matches any port. `IETF_LLM_MCP_ALLOWED_ORIGINS` likewise
    restricts the `Origin` header (browser callers); unset means any origin.
    This lets an operator front the server directly (no proxy enforcing Host)
    without exposure to DNS-rebinding, which is otherwise the proxy's job.
    """
    from mcp.server.transport_security import (  # pylint: disable=import-outside-toplevel,import-error
        TransportSecuritySettings,
    )

    allowed_hosts = _csv_env("IETF_LLM_MCP_ALLOWED_HOSTS")
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=_csv_env("IETF_LLM_MCP_ALLOWED_ORIGINS"),
    )


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
    allowed_hosts = _csv_env("IETF_LLM_MCP_ALLOWED_HOSTS")
    return {
        "transport": "http",
        "bind": f"{host}:{port}",
        "stateless": "yes" if _stateless_http_enabled() else "no",
        "gather": "on" if _gather_enabled() else "off",
        "embed_backend": backend,
        "embed_model": model,
        "no_embed": "yes" if _effective_no_embed() else "no",
        "index_dir": get_index_dir(),
        "index_immutable": "yes" if _index_immutable_enabled() else "no",
        "store_backend": service_config.store_backend(),
        "host_allowlist": ",".join(allowed_hosts) if allowed_hosts else "off",
        "log_level": _log_verbosity().name.lower(),
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
                ("IETF_LLM_STORE_URL", service_config.store_url()),
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
