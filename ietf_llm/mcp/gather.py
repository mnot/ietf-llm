"""Gather tools (write + network): start_gather, gather_status,
stop_gather, suggest_github_repos, get_session_log."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .. import _debug_log
from ..utils import months_request_caution, months_request_error
from .common import (
    _corpus_exists,
    _gather_elapsed,
    _offload,
    _stage_phrase,
    _tool_timeout_seconds,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


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
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

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
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

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
        phrase = _stage_phrase(final)
        elapsed = _gather_elapsed(final) if final else ""
        where = "; ".join(p for p in (phrase, elapsed) if p)
        at = f" ({where})" if where else ""
        return (
            f"Waited ~{int(budget)}s; still gathering{at} — the embedding-index / "
            f"topic-map tail is the slow part, so it could take a few more "
            f"minutes. Tell the user and offer to check back once `gather_status` "
            f"reports `done`; reads before then are stale or partial. Block with "
            f'`gather_status(corpus="{corpus}", wait=60)` rather than reading now. '
            f"{out}"
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
            "few at once to stay polite to upstreams (Datatracker / GitHub), so "
            f"it starts when a slot frees. Poll "
            f'`gather_status(corpus="{corpus}")`.{stop_hint}'
        )
    first = not _corpus_exists(corpus)
    timing = (
        "a first gather of a corpus can take minutes"
        if first
        else "re-gathers are usually quick — only new material is fetched"
    )
    user_note = (
        " This is a one-time cold fetch that could take a few minutes; tell the "
        "user that and offer to check back once it reports `done`, rather than "
        "waiting silently."
        if first
        else ""
    )
    return (
        f"Started gathering '{corpus}' in the background ({timing}). Poll "
        f'`gather_status(corpus="{corpus}")` for stage-level progress; the '
        f"corpus is queryable once it reports `done` (reads before then are "
        f"stale or partial).{user_note}{stop_hint}"
    )


def tool_gather_status(
    corpus: Optional[str] = None, wait: Optional[float] = None
) -> str:
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

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
    from .. import gather_runner  # pylint: disable=import-outside-toplevel

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


def tool_suggest_github_repos(corpus: str) -> str:
    from .. import gather_runner  # pylint: disable=import-outside-toplevel
    from ..gather.repo_discovery import (  # pylint: disable=import-outside-toplevel
        discover_group_repos,
        format_discovery,
    )

    corpus = (corpus or "").strip()
    if not corpus:
        return "Provide a Working Group shortname (e.g. `tls`) to discover repos for."
    if not gather_runner.valid_corpus_name(corpus):
        return f"'{corpus}' is not a valid corpus name."
    return format_discovery(discover_group_repos(corpus))


def register(server: "FastMCP") -> None:
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
        (the stage it reached, e.g. `stage 18/19 (embedding index)`) plus a
        stop token — then poll `gather_status(corpus=...)` until it reports
        `done`. Pass `wait=0` to return immediately instead (fire-and-forget:
        kick off the gather and do other work), or a custom `wait` (seconds)
        to block longer or shorter. Later re-gathers fetch only what changed
        since last time.

        The corpus isn't queryable until `done`. While a **first** gather
        runs the read tools refuse (there's no prior snapshot to serve),
        telling you the stage and how many are left — so poll rather than
        read early. A **re-gather** keeps serving the previous snapshot
        (flagged as a refresh in progress), so querying it meanwhile is fine.

        The corpus **shape is inferred** from what you pass — you don't
        declare it:
        - **Working Group / RG / BoF**: pass just `corpus` as the
          shortname (`tls`, `cfrg`). The charter, drafts, meetings,
          mailing list, and GitHub issues are auto-discovered (the WG's
          published RFCs are listed in the overview but their bodies stay
          in the global series — `search_rfcs` / `get_rfc`)
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

        One gather per corpus runs at a time — so a call while it's in
        flight reports "already running". A *different* corpus runs
        concurrently up to a small cap; beyond that it reports
        "queued". Either way, poll `gather_status`,
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


def register_session_log(server: "FastMCP") -> None:
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
