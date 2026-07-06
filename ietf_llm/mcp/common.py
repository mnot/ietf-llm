"""Shared scaffolding for the ietf-llm MCP tools: corpus-path
resolution, the ``@_requires_corpus`` guard, freshness/inflight notes, the
grounding + participation nudges, and the ``_offload`` dispatcher."""

from __future__ import annotations

import datetime
import functools
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import anyio

from .. import _debug_log, coverage, serve_metrics
from ..access import note_access
from ..store.corpus import VersionVanished, get_corpus_store, pin_corpus_version
from ..digest.query import parse_md_tables
from ..embeddings import probe_index
from ..freshness import (
    deployment_mode,
    freshness_line,
    gather_enabled,
    gather_suggestion,
    last_gathered,
)
from ..paths import digest_path
from ..positions import file_supports_tally
from ..utils import LogLevel, Verbosity, log

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
        # Refuse rather than serve half-built content while a corpus's *first*
        # gather runs — there is no prior snapshot to fall back on, so the cache
        # is being built in place. A re-gather is not guarded (it keeps serving
        # the previous complete version).
        first_gather = _first_gather_guard(wg)
        if first_gather is not None:
            return first_gather
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


def _stage_phrase(status: Optional[Dict[str, Any]]) -> Optional[str]:
    """`stage N/M (name)` for a running gather status, or None before any stage
    is entered. Shared by the still-running / timeout / refusal messages so they
    all name the same point in the pipeline (`stage_index` starts at 0)."""
    if not status:
        return None
    idx = status.get("stage_index") or 0
    total = status.get("stage_total")
    stage = status.get("stage")
    if total and idx:
        return f"stage {idx}/{total}" + (f" ({stage})" if stage else "")
    if stage:
        return f"stage: {stage}"
    return None


def _first_gather_guard(wg: str) -> Optional[str]:
    """Refuse a read while the corpus's *first* gather is still running, else
    None.

    A first gather has no previously-published snapshot to fall back on, so the
    cache is being built in place and any read would serve half-built content
    that reads like a real answer. Refuse, naming the stage, rather than return
    it. A re-gather (a completed version exists — `last_gathered` is set) is not
    guarded here: it keeps serving the previous complete snapshot with the
    in-flight caveat from `_inflight_refresh_note`."""
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

    status = gather_runner.local_inflight(wg)
    if not status or last_gathered(wg) is not None:
        return None
    phrase = _stage_phrase(status) or "starting"
    total = status.get("stage_total")
    idx = status.get("stage_index") or 0
    left = f", {total - idx} stage(s) left" if total and idx else ""
    elapsed = _gather_elapsed(status)
    so_far = f", {elapsed} so far" if elapsed else ""
    return (
        f"First gather of '{wg}' is still in progress ({phrase}{left}{so_far}); "
        "the corpus is not queryable yet — its search index and digests are built "
        "in the final stages. This is a one-time cold fetch that could take a few "
        "minutes; tell the user that and offer to check back once it reports "
        f'`done`, rather than waiting silently. Call `gather_status(corpus="{wg}", '
        "wait=60)` to block until it reports `done`, then retry."
    )


def _index_rebuilding_note(wg: str) -> Optional[str]:
    """Caveat for an empty index-backed read when a gather is running and the
    index cannot be served yet (absent / mid-rebuild), else None.

    With the atomic index swap a re-gather keeps the previous index servable
    throughout, so this is a safety net (e.g. a legacy in-place index that never
    completed): it turns a bare "no results" into "not ready yet"."""
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

    status = gather_runner.local_inflight(wg)
    if not status or probe_index(wg):
        return None
    phrase = _stage_phrase(status) or "in progress"
    return (
        f"the search index for '{wg}' is being rebuilt ({phrase}) and isn't ready "
        f'yet — retry once `gather_status(corpus="{wg}")` reports `done`'
    )


def _timeout_inflight_note(
    fn: Callable[..., str], args: "tuple[Any, ...]"
) -> Optional[str]:
    """A clearer read-timeout message when the slow call was a per-corpus read
    and a gather for that corpus is running, else None.

    Names the stage so "still gathering" is distinguishable from "server fell
    over" — the exact ambiguity a client hits when a read stalls mid-gather. The
    gather runs in a subprocess, so the server stays responsive and this
    deadline fires cleanly; the read was slow because it touched the cache
    mid-rebuild."""
    if not args:
        return None
    corpus = args[0]
    if not isinstance(corpus, str) or not corpus.strip():
        return None
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

    if not gather_runner.valid_corpus_name(corpus):
        return None
    status = gather_runner.local_inflight(corpus)
    if not status:
        return None
    phrase = _stage_phrase(status) or "in progress"
    return (
        f"(The read timed out because a gather for '{corpus}' is still running "
        f"({phrase}) — not because the server is unresponsive. It is serving the "
        f'previous snapshot; retry once `gather_status(corpus="{corpus}")` '
        "reports `done`.)"
    )


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
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

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
        "a mailing-list message, a GitHub issue or comment, or any reply that goes into "
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


def _deployment_phrase() -> str:
    """One clause stating this server's topology (only) — fixed for its lifetime.
    The gather-cost implication lives in `_gather_cost_clause`, since it only
    applies when gather is actually available (a read-only server has no cost to
    weigh). Shared by the session block and the `list_corpora` footer."""
    if deployment_mode() == "http":
        return "a shared HTTP server (may be multi-user)"
    return "a local, single-user stdio server"


def _gather_cost_clause() -> str:
    """The cost caution for a gather, scoped to the topology — rendered only when
    gather is available, so read-only mode never claims a gather is 'free'."""
    if deployment_mode() == "http":
        return (
            "a wide gather fan-out can cost other users, so gather deliberately "
            "(only the efforts that dominate the question)"
        )
    return "a gather costs only you, so gather freely, no permission needed"


def _capability_phrase() -> str:
    """One terse clause stating whether in-session gather + live Datatracker
    lookups are available here (they share one gate). The how-to lives in the
    guidance paragraph of `_session_section`, mode-composed so a read-only
    server never names `start_gather` and a stdio server never names cloud."""
    if gather_enabled():
        return "in-session gather and live Datatracker lookups are available here"
    return "read-only (in-session gather and live Datatracker lookups are off)"


def _gather_brief() -> str:
    """The gather half of `_capability_phrase`, trimmed for the `list_corpora`
    footer (which only needs the add-a-corpus decision, not the live-tool
    detail); carries the topology-scoped cost only when gather is available."""
    if gather_enabled():
        return (
            'in-session gather is available (`start_gather(corpus="<name>")`; '
            f"tool-search for it if your client has not loaded it) — "
            f"{_gather_cost_clause()}"
        )
    return (
        "read-only — no `start_gather`; to add a corpus have the user run "
        "`ietf-llm <name>` locally"
    )


def _session_section() -> str:
    """The whole mode-specific region of the `instructions` field, injected at
    `{{SESSION}}` and composed once at startup. States deployment + capability as
    facts, then a single guidance paragraph written for the actual mode — so a
    read-only server never mentions `start_gather` anywhere and a stdio server
    never mentions cloud/shared operation. The (mode-neutral) tool descriptions
    below carry none of this, so nothing forces the client to infer its mode."""
    facts = (
        "Fixed for this server's lifetime — read these here, don't infer them "
        "from the tool descriptions below:\n\n"
        f"- **Deployment:** {_deployment_phrase()}.\n"
        f"- **Capability:** {_capability_phrase()}."
    )
    if gather_enabled():
        guidance = (
            "To add or refresh a corpus, `start_gather(corpus=…)` — "
            f"{_gather_cost_clause()}. **Set the user's expectations up front, "
            "as you announce the gather:** a first gather is often a minute or "
            "two, and several minutes for a very active group — say so, rather "
            "than announcing it and going quiet (the call itself blocks for a "
            "bounded wait, ~90s, before it even returns). It returns naming the "
            "stage and elapsed time; the corpus is queryable once `gather_status` "
            "reports `done`, and reads refuse until then (a re-gather keeps "
            "serving the previous snapshot); poll `gather_status(corpus=…, "
            "wait=60)` and keep the user posted rather than going silent. For "
            "live, daily-changing facts use `draft_status` / `meeting_schedule`; "
            "otherwise the offline "
            "`list_drafts` / `list_meetings` / `read_minutes`."
        )
    else:
        guidance = (
            "This server can't gather, and has no live Datatracker lookups. To "
            "add a corpus `list_corpora` doesn't show, tell the user to run "
            "`ietf-llm <name>` locally, then query it here; for draft and meeting "
            "facts use the offline `list_drafts` / `list_meetings` / "
            "`read_minutes`."
        )
    return f"{facts}\n\n{guidance}"


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
    inflight = _timeout_inflight_note(fn, args)
    if inflight is not None:
        return inflight
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


def _resolve_transport() -> str:
    """Return the selected MCP transport: 'http' or 'stdio' (default).

    stdio stays the default for local use; the shared-server deployment
    sets IETF_LLM_MCP_TRANSPORT=http (or 'streamable-http').
    """
    transport = os.environ.get("IETF_LLM_MCP_TRANSPORT", "stdio").strip().lower()
    return "http" if transport in ("http", "streamable-http") else "stdio"


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
