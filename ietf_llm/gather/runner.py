"""Background gather orchestration for the MCP server.

The MCP server is otherwise read-only and never touches the network; this
module is the one deliberate exception, gated by `mcp.common._gather_enabled`
(on by default for a local stdio server, off for the shared HTTP deployment;
`IETF_LLM_ENABLE_GATHER` overrides either way). A `start_gather`
tool call returns immediately, enqueuing the request; a background worker thread
runs the same pipeline as the `ietf-llm` CLI — out of process, via
`pipeline` (so a CPU-heavy stage can't stall the server) — and records
stage-level progress to a per-corpus status record
(`~/.cache/ietf-llm/<corpus>/gather-status.json` and, on the cloud backend, the
control plane) that the `gather_status` tool reads back. This module owns the
queue, the shared heartbeat and cooperative cancel; `lifecycle` owns the
store-facing steps around the pipeline (lease and slot TTLs, seed/hydrate,
publish/persist, the status record) that the `ietf-llm` CLI shares; `pipeline`
owns running the pipeline and streaming its progress back into the worker's
callbacks.

Concurrency model — `N = service_config.gather_max_inflight()` (default 3) caps
gather concurrency two ways, keeping the pipeline polite to shared upstreams
(datatracker, mailarchive, GitHub) while letting a second client's gather start
without waiting behind the first:

- **Per host:** a pool of N workers drains a bounded FIFO queue, so a host runs
  up to N gathers at once; beyond that, requests queue (an in-process registry
  answers "already running / queued here?" and bounds the backlog).
- **Fleet-wide:** each running gather holds one of N per-job global slots in the
  control plane, so the whole deployment runs at most N at once — a single host
  can use all N, a multi-host fleet shares them. On the local backend the slot
  is a no-op grant, so only the per-host pool applies.

The gather pipeline is safe to run concurrently per corpus (HTTP egress metrics
are thread-local, and each corpus has its own cache dir). The per-corpus lease
(cloud backend) is taken at *enqueue*, so it dedupes the same corpus across
hosts and anchors the `queued`/`running` record's liveness.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .. import freshness, serve_metrics
from ..corpus import canonical
from ..config import service as service_config
from ..log import LogLevel, Verbosity, log
from ..paths import get_cache_dir, valid_corpus_name
from ..store.corpus import LeaseLost
from . import lifecycle, pipeline

# `GatherCancelled` lives in `pipeline` (the raiser) and is re-exported here so
# callers keep catching it as `gather_runner.GatherCancelled` (this module's
# public alias). `pipeline` is light (it defers the heavy pipeline import to
# gather time) and never imports `runner` — the cancel check is passed in as a
# callback — so the dependency runs one way.
from .pipeline import GatherCancelled

#: Minimum seconds between cooperative cancel polls on a chatty stage. A stage
#: transition always polls (they are infrequent); mid-stage detail updates poll
#: at most this often so a per-batch reporter does not hammer the control plane.
_CANCEL_POLL_S = 4.0

#: Bytes of entropy in a gather's stop token. `secrets.token_urlsafe` renders
#: ~1.3 chars per byte, so 9 bytes is a ~12-char token — unguessable enough to
#: keep one client from stopping another's gather, short enough to echo back.
_CANCEL_TOKEN_BYTES = 9

#: Default bound on this host's pending-gather backlog (queued + running);
#: overflow is refused so a runaway caller cannot pile up unbounded work.
_DEFAULT_QUEUE_MAX = 16

# This host runs up to N gathers at once (the per-host cap, N =
# service_config.gather_max_inflight()): a pool of N worker threads drains a
# shared FIFO queue, and each running gather holds a per-job global slot in the
# control plane (the fleet-wide cap — N slots total, so the whole deployment
# never exceeds N either). `_jobs` maps corpus -> state ("queued" | "running" |
# "finishing") for in-process dedup, queue-bound enforcement, and local-backend
# liveness. "finishing" is set just before the terminal status is written and
# held until the worker has released the slot, lease and registry entry.
_queue: "queue.Queue[GatherSpec]" = queue.Queue()
_jobs: Dict[str, str] = {}
_registry_lock = threading.Lock()
#: Signalled (under `_registry_lock`) when a worker drops a corpus from `_jobs`,
#: so `start` can wait out a "finishing" job instead of refusing it.
_registry_cond = threading.Condition(_registry_lock)
#: How long `start` waits for a finishing job's cleanup (a few store round-trips)
#: before giving up with the ordinary "already running" refusal.
_FINISH_WAIT_S = 10.0
#: The worker pool, mutated in place so it needs no module-level `global` rebind.
_workers: "List[threading.Thread]" = []
_heartbeat_stop = threading.Event()
#: The single lease/slot heartbeat thread. Tracked so a pool regrow (after a
#: worker died) doesn't spawn a second heartbeat alongside the first.
_heartbeat_thread: "Optional[threading.Thread]" = None  # pylint: disable=invalid-name

#: Cooperative-cancel state, keyed by corpus, guarded by `_registry_lock`.
#: `_cancel_tokens` holds the SHA-256 of each in-flight gather's stop token so
#: `request_stop` can authenticate a request without the raw token being stored.
#: `_cancel_events` is a same-process fast path: a stop landing on the host
#: running the gather sets the event (race-free); a stop on another host (cloud
#: backend) instead rides the published status record's `cancel_requested` flag,
#: which the worker polls — best-effort cross-host (see `request_stop`).
_cancel_tokens: Dict[str, str] = {}
_cancel_events: Dict[str, threading.Event] = {}
#: Each in-flight gather's view of its lease, keyed by corpus, guarded by
#: `_registry_lock`. The shared heartbeat feeds it; the job's worker reads
#: `lost` to stop (and not publish) once another host has taken the corpus.
_lease_watches: Dict[str, lifecycle.LeaseWatch] = {}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cancel_requested(corpus: str) -> bool:
    """True if a stop has been requested for `corpus`: the in-process event (same
    host) or the published status's `cancel_requested` flag (cross-host)."""
    with _registry_lock:
        event = _cancel_events.get(corpus)
    if event is not None and event.is_set():
        return True
    status = read_status(corpus)
    return bool(status and status.get("cancel_requested"))


@dataclass
class GatherSpec:
    """A gather request: corpus name plus the same optional sources/scope
    the `ietf-llm` CLI accepts. The pipeline classifies the shape (group /
    list / custom / synthetic) from these — callers don't say which."""

    corpus: str
    mailing_list: List[str] = field(default_factory=list)
    draft: List[str] = field(default_factory=list)
    github: List[str] = field(default_factory=list)
    github_label: List[str] = field(default_factory=list)
    exclude_github_label: List[str] = field(default_factory=list)
    author: Optional[str] = None
    new_drafts: bool = False
    months: Optional[int] = None
    add_mentioned_drafts: bool = False
    include_related_drafts: bool = False
    #: Bypass the freshness debounce (re-gather even if recently gathered).
    #: start() checks it directly; to_argv also renders it so the background
    #: thread's _gather_one honours the same bypass.
    force: bool = False

    def has_sources(self) -> bool:
        """True if this request supplies any source/scope flags — i.e. it is
        more than a plain refresh. Such a request bypasses the freshness
        debounce, since the caller is changing *what* gets gathered, not
        just asking to refresh the same snapshot."""
        return bool(
            self.mailing_list
            or self.draft
            or self.github
            or self.github_label
            or self.exclude_github_label
            or self.author
            or self.new_drafts
            or self.add_mentioned_drafts
            or self.include_related_drafts
        )

    def to_argv(self) -> List[str]:
        """Render as CLI-style argv for `sequencer.run_gather`."""
        argv: List[str] = [self.corpus]
        for value in self.mailing_list:
            argv += ["--mailing-list", value]
        for value in self.draft:
            argv += ["--draft", value]
        for value in self.github:
            argv += ["--github", value]
        for value in self.github_label:
            argv += ["--github-label", value]
        for value in self.exclude_github_label:
            argv += ["--exclude-github-label", value]
        if self.author:
            argv += ["--author", self.author]
        if self.new_drafts:
            argv.append("--new-drafts")
        if self.months is not None:
            argv += ["--months", str(self.months)]
        if self.add_mentioned_drafts:
            argv.append("--add-mentioned-drafts")
        if self.include_related_drafts:
            argv.append("--include-related-drafts")
        if self.force:
            # Propagate the bypass to the background thread's _gather_one,
            # which re-checks the debounce; start() already decided to run.
            argv.append("--force")
        # An MCP-initiated gather is an agent/server context — it never needs
        # the local grep / NotebookLM raw/ dumps, nor the slide-deck .pdf
        # sources (the indexed .pdf.txt is kept). Always suppress both so the
        # served version stays lean.
        argv += ["--no-raw", "--no-pdf"]
        return argv

    def to_dict(self) -> Dict[str, Any]:
        """The non-default fields, for embedding in the status file."""
        out: Dict[str, Any] = {"corpus": self.corpus}
        for key in (
            "mailing_list",
            "draft",
            "github",
            "github_label",
            "exclude_github_label",
        ):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        for key in (
            "author",
            "new_drafts",
            "months",
            "add_mentioned_drafts",
            "include_related_drafts",
        ):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


def _pre_start_refusal(spec: GatherSpec) -> Optional[Dict[str, Any]]:
    """Reason to refuse a gather *before* queuing, or None to proceed.

    The pre-queue gate, in priority order: a gather already in flight (here
    or, on the cloud backend, on another host), then — unless `force` — a
    source-duplicating custom corpus, then the freshness debounce.
    """
    corpus = spec.corpus
    # Cross-host guard (cloud backend): a gather already queued or running on
    # another host is fleet-visible through the control plane. Refuse up front so
    # the caller just polls `gather_status` and sees that host's live progress.
    # `force` overrides the freshness debounce only — never a gather in flight.
    # None on the local backend, where the in-process registry catches same-host
    # races (the lease below is the cross-host arbiter for the tight race).
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    fleet = corpus_store.get_corpus_store().get_gather_status(corpus)
    if fleet is not None and fleet.get("state") in ("queued", "running"):
        return {"started": False, "reason": "already running", "corpus": corpus}
    if spec.force:
        return None
    # Canonicalisation: a new custom/synthetic corpus that duplicates an
    # existing one's sources is steered to reuse rather than minted.
    hint = canonical.mcp_canonicalize_skip(spec)
    if hint is not None:
        return {
            "started": False,
            "reason": "similar exists",
            "detail": hint,
            "corpus": corpus,
        }
    # Freshness debounce: a plain re-gather of a recently-gathered corpus is
    # skipped. A source-changing request isn't a plain refresh.
    if not spec.has_sources():
        # The store's gathered time, not the local sentinel: a replica that has
        # not seeded this corpus yet has no sentinel, would admit the request,
        # and would download the published version only to find it fresh.
        detail = freshness.debounce_reason(
            corpus, gathered_at=corpus_store.get_corpus_store().gathered_at
        )
        if detail is not None:
            return {
                "started": False,
                "reason": "fresh",
                "detail": detail,
                "corpus": corpus,
            }
    return None


def _queue_max() -> int:
    """Bound on this host's pending-gather backlog (queued + running). Overflow
    is refused so a runaway caller cannot pile up unbounded background work.
    `IETF_LLM_GATHER_QUEUE_MAX` overrides the default; sub-1 falls back."""
    raw = os.environ.get("IETF_LLM_GATHER_QUEUE_MAX", "").strip()
    if not raw:
        return _DEFAULT_QUEUE_MAX
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_QUEUE_MAX
    return value if value >= 1 else _DEFAULT_QUEUE_MAX


def start(  # pylint: disable=too-many-return-statements
    spec: GatherSpec,
) -> Dict[str, Any]:
    """Enqueue a background gather for `spec.corpus`.

    Returns `{"started": True, "corpus": ..., "queued_behind": N}` when the
    gather was accepted (N gathers already in flight ahead of it on this host;
    0 means it starts as soon as a fleet slot is free), or
    `{"started": False, "reason": ...}` otherwise: `"already running"` (one is
    in flight for this corpus, here or — cloud backend — on another host),
    `"similar exists"` / `"fresh"` (unforced-only debounce hints, with a
    `"detail"`), or `"queue full"` (this host's backlog is at the bound).
    Returns immediately; progress is tracked via the status record.

    One gather runs at a time on this host (a single worker drains the queue),
    and at most `service_config.gather_max_inflight()` run across the whole
    deployment at once (a global slot in the control plane). The per-corpus
    lease is taken here, at enqueue, so it dedupes across hosts and anchors the
    queued job's liveness.
    """
    corpus = spec.corpus
    if not valid_corpus_name(corpus):
        return {"started": False, "reason": "invalid name", "corpus": corpus}
    refusal = _pre_start_refusal(spec)
    if refusal is not None:
        return refusal
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    store = corpus_store.get_corpus_store()
    owner = _owner()
    with _registry_lock:
        # The terminal status is published before the worker frees the slot,
        # lease and registry entry, so a client restarting on seeing it lands
        # here mid-cleanup. Wait for the old job to be gone: admitting the new
        # one earlier would let the old worker's cleanup release the new job's
        # lease and slot, which share the old one's owner.
        _registry_cond.wait_for(
            lambda: _jobs.get(corpus) != "finishing", timeout=_FINISH_WAIT_S
        )
        if corpus in _jobs:
            return {"started": False, "reason": "already running", "corpus": corpus}
        if len(_jobs) >= _queue_max():
            return {"started": False, "reason": "queue full", "corpus": corpus}
        # Take the per-corpus lease NOW, before queuing: on the cloud backend
        # this atomically dedupes the same corpus across hosts (the loser of a
        # near-simultaneous race is told "already running"), and it is the
        # liveness anchor for the queued state. A no-op grant on the local
        # backend, where the in-process registry above is the dedup.
        if not store.acquire_lease(corpus, owner, lifecycle.LEASE_TTL):
            return {"started": False, "reason": "already running", "corpus": corpus}
        ahead = len(_jobs)
        _jobs[corpus] = "queued"
        # Mint this gather's stop token. Only its hash is persisted (in the
        # status record, below) so the raw token returned to the caller is the
        # sole capability that can stop it. The event is the same-host fast path.
        token = secrets.token_urlsafe(_CANCEL_TOKEN_BYTES)
        token_hash = _hash_token(token)
        _cancel_tokens[corpus] = token_hash
        _cancel_events[corpus] = threading.Event()
        _lease_watches[corpus] = lifecycle.LeaseWatch(corpus)
    # Publish `queued` BEFORE enqueueing, then enqueue, then ensure the worker is
    # up. Ordering matters: the worker only ever writes `running`/terminal, and
    # it can only dequeue after the put below — so this `queued` write always
    # happens-before the worker's writes and can never clobber a later state
    # (which, with a fast gather, would otherwise leave the record stuck at
    # `queued`). It also makes the queued state fleet-visible at once (a client
    # checking status on another replica sees it, not just the enqueuing host).
    queued_status = _new_status(spec, "queued")
    queued_status["cancel_token_sha256"] = token_hash
    _write_status(store, queued_status)
    try:
        with _registry_lock:
            _queue.put(spec)
            _ensure_worker(owner)
    except BaseException:  # pylint: disable=broad-except
        # Enqueue or worker spawn failed (e.g. RuntimeError under thread/FD
        # exhaustion). The worker's finally is the only place that frees the
        # reservation, and no worker will ever run this spec — so roll back here
        # or the corpus is stranded as 'already running' with its lease held
        # forever.
        with _registry_lock:
            _jobs.pop(corpus, None)
            _cancel_tokens.pop(corpus, None)
            _cancel_events.pop(corpus, None)
            _lease_watches.pop(corpus, None)
        try:
            store.release_lease(corpus, owner)
        except Exception:  # pylint: disable=broad-except
            pass
        return {"started": False, "reason": "could not start gather", "corpus": corpus}
    # How many gathers this one actually waits behind: with N running
    # concurrently, a free slot exists until `ahead` reaches N, so anything under
    # that starts at once (wait 0). Reported so the tool can say "started" vs
    # "queued behind k" truthfully.
    wait_for = max(0, ahead - service_config.gather_max_inflight() + 1)
    return {
        "started": True,
        "queued_behind": wait_for,
        "corpus": corpus,
        "cancel_token": token,
    }


def request_stop(corpus: str, token: str) -> Dict[str, Any]:
    """Request cancellation of an in-flight gather for `corpus`.

    Authenticated by the `token` `start` returned — only its holder can stop the
    gather, so one client cannot cancel another's. Cooperative and best-effort:
    the worker honours it at its next stage boundary or download batch (see
    `_progress`), so this reports "stopping", not "stopped". The same-host case
    is race-free (an in-process event); cross-host (cloud backend) rides the
    published `cancel_requested` flag, which the worker polls — a stop set in the
    brief window before the worker republishes its status can be missed and need
    re-issuing.

    Returns `{"stopped": True, "corpus": ...}` when accepted, else
    `{"stopped": False, "reason": ...}`: `"invalid name"`, `"not running"` (no
    active gather), or `"bad token"` (missing or incorrect token)."""
    if not valid_corpus_name(corpus):
        return {"stopped": False, "reason": "invalid name", "corpus": corpus}
    status = read_status(corpus)
    if status is None or status.get("state") not in ("queued", "running"):
        return {
            "stopped": False,
            "reason": "not running",
            "corpus": corpus,
            "state": status.get("state") if status else None,
        }
    expected = status.get("cancel_token_sha256")
    if (
        not token
        or not expected
        or not secrets.compare_digest(_hash_token(token), str(expected))
    ):
        return {"stopped": False, "reason": "bad token", "corpus": corpus}
    # Same-host fast path: set the in-process event (race-free). Always also
    # write the flag so a worker on another host (cloud backend) sees it.
    with _registry_lock:
        event = _cancel_events.get(corpus)
    if event is not None:
        event.set()
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    store = corpus_store.get_corpus_store()
    status["cancel_requested"] = True
    _write_status(store, status)
    return {"stopped": True, "corpus": corpus, "state": status.get("state")}


def _new_status(spec: GatherSpec, state: str) -> Dict[str, Any]:
    return lifecycle.new_status(spec.corpus, spec.to_dict(), state, driver="mcp")


_write_status = lifecycle.write_status
_status_path = lifecycle.status_path
_now_iso = lifecycle.now_iso


def _store() -> Any:
    """The current CorpusStore. Fetched fresh (not captured) so the long-lived
    worker/heartbeat always honour the live config rather than whatever was set
    when they first started."""
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    return corpus_store.get_corpus_store()


_slot_owner = lifecycle.slot_owner
_owner = lifecycle.process_owner


def _ensure_worker(owner: str) -> None:
    """Ensure the gather worker pool is at least `gather_max_inflight()` strong,
    and a lease/slot heartbeat is running. Caller holds `_registry_lock`. All are
    daemons that live for the process. Grow-only: it never shrinks the pool (you
    cannot un-start a thread), so in steady state — a fixed N — it grows once and
    returns early thereafter; the fleet slot is the authoritative concurrency
    cap, the pool just needs to be big enough not to be the bottleneck."""
    global _heartbeat_thread  # pylint: disable=global-statement
    alive = [w for w in _workers if w.is_alive()]
    target = max(1, service_config.gather_max_inflight())
    if len(alive) >= target:
        return
    _heartbeat_stop.clear()
    _workers[:] = alive
    for i in range(len(alive), target):
        worker = threading.Thread(
            target=_worker_loop, args=(owner,), name=f"gather-worker-{i}", daemon=True
        )
        _workers.append(worker)
        worker.start()
    # Exactly one heartbeat: only (re)start it when none is running, so a pool
    # regrow doesn't leave two heartbeats racing on the shared stop event.
    if _heartbeat_thread is None or not _heartbeat_thread.is_alive():
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(owner, _heartbeat_stop),
            name="gather-heartbeat",
            daemon=True,
        )
        _heartbeat_thread.start()


def _heartbeat_loop(owner: str, stop: threading.Event) -> None:
    """Renew every lease this host holds (queued + running jobs) and each running
    gather's slot, comfortably within the TTL, so a long gather — or a long wait
    for a fleet slot — cannot let the lease/slot lapse. No-ops on the local
    backend. Lives for the process (daemon) and is the one heartbeat for every
    job on the host, so nothing in a beat may end it; `stop` is a test seam."""
    while not stop.wait(lifecycle.LEASE_HEARTBEAT_S):
        try:
            _heartbeat_once(owner)
        except Exception as err:  # pylint: disable=broad-except
            log(
                f"gather.runner: heartbeat error: {type(err).__name__}: {err}",
                Verbosity.STATUS,
                level=LogLevel.WARN,
            )


def _heartbeat_once(owner: str) -> None:
    """One beat: renew each job's lease, marking the job's own `LeaseWatch` when
    its lease is lost, so one corpus losing its lease stops only that gather. A
    job already finishing is skipped: its worker is about to release the lease,
    and a renewal landing after the release would revive it."""
    with _registry_lock:
        items = [(c, st, _lease_watches.get(c)) for c, st in _jobs.items()]
    if not items:
        return
    store = _store()
    for corpus, state, watch in items:
        if state == "finishing" or watch is None or watch.lost.is_set():
            continue
        watch.renew(store, owner)
        if state == "running" and not watch.lost.is_set():
            lifecycle.renew_slot(store, _slot_owner(owner, corpus))


def _worker_loop(owner: str) -> None:
    """One worker of the pool: take the next queued job and run it (so up to N
    workers run N gathers at once). Each job already holds its per-corpus lease
    (taken at enqueue); the worker waits for a fleet slot, runs the pipeline,
    then releases slot and lease."""
    while True:
        spec = _queue.get()
        corpus = spec.corpus
        store = _store()
        try:
            _run_one(store, owner, spec)
        except BaseException as err:  # pylint: disable=broad-except
            # The worker must outlive any single job's failure.
            log(
                f"gather.runner: worker error on {corpus}: {err}",
                Verbosity.STATUS,
                level=LogLevel.ERROR,
            )
        finally:
            try:
                store.release_lease(corpus, owner)
            except Exception:  # pylint: disable=broad-except
                pass
            with _registry_lock:
                _jobs.pop(corpus, None)
                _cancel_tokens.pop(corpus, None)
                _cancel_events.pop(corpus, None)
                _lease_watches.pop(corpus, None)
                _registry_cond.notify_all()
            _queue.task_done()


def _mark_finishing(corpus: str) -> None:
    """Flag a registered job as past its work, before its terminal status is
    published: the corpus is no longer live (`local_inflight`), but `start`
    still holds off until the worker has finished cleaning up."""
    with _registry_lock:
        if corpus in _jobs:
            _jobs[corpus] = "finishing"


def _run_one(store: Any, owner: str, spec: GatherSpec) -> None:
    """Run one queued gather: wait for a fleet-wide slot, then run the pipeline,
    publishing status at each transition. Holds the per-corpus lease throughout
    (released by the worker loop). Never raises — failures become a `failed`
    status. The pipeline itself runs in a subprocess (`pipeline`), so this
    worker thread only orchestrates and streams progress — a CPU-heavy stage
    never holds the GIL here and stalls the server."""
    corpus = spec.corpus
    slot_owner = _slot_owner(owner, corpus)
    status = _new_status(spec, "queued")
    # Carry the stop-token hash minted at enqueue (same process) so request_stop
    # can authenticate a cancel against the record the worker keeps republishing.
    with _registry_lock:
        status["cancel_token_sha256"] = _cancel_tokens.get(corpus)
        watch = _lease_watches.get(corpus) or lifecycle.LeaseWatch(corpus)
    # Wait for one of the fleet-wide gather slots. While queued, the heartbeat
    # renews the lease; re-publishing `queued` keeps `updated` fresh so a stale
    # record is distinguishable. On shutdown, bail without a terminal status —
    # the released lease lets liveness relabel the record `interrupted`.
    max_inflight = service_config.gather_max_inflight()
    while not store.acquire_gather_slot(
        slot_owner, corpus, lifecycle.LEASE_TTL, max_inflight
    ):
        if _heartbeat_stop.is_set():
            return
        if watch.lost.is_set():
            # Another host holds the corpus now; the fleet record is theirs.
            _fail_lease_lost(store, status, watch, corpus)
            return
        if _cancel_requested(corpus):
            # Stopped before it ever got a slot — no slot/pipeline to unwind,
            # so finalise the terminal record directly.
            status["state"] = "cancelled"
            status["cancel_requested"] = True
            status["finished"] = _now_iso()
            _mark_finishing(corpus)
            _write_status(store, status)
            return
        _write_status(store, status)
        time.sleep(lifecycle.SLOT_POLL_S)
    # Gather lifecycle for /metrics: started once we go `running`, finished in
    # the outer `finally` below. `run_start` stays None until then so the
    # finally only records (and balances the in-flight gauge) for a gather that
    # actually started — and it does so for *every* such gather, even if seed /
    # hydrate raise before the pipeline's own try/finally.
    run_start: Optional[float] = None
    try:
        with _registry_lock:
            _jobs[corpus] = "running"
        status["state"] = "running"
        _write_status(store, status)
        serve_metrics.record_gather_started()
        run_start = time.monotonic()

        recorder = lifecycle.StatusRecorder(store, status)
        last_cancel_poll = 0.0

        def _progress(
            name: str, index: int, total: int, detail: Optional[str] = None
        ) -> None:
            nonlocal last_cancel_poll
            if watch.lost.is_set():
                # Checked on every call (an Event read is free), ahead of the
                # polled cancel check: a zombie gather should stop at the next
                # checkpoint, not up to `_CANCEL_POLL_S` later.
                raise GatherCancelled(corpus)
            now = time.monotonic()
            # Cooperative cancellation checkpoint. Reached at every stage
            # boundary (detail is None) and every download batch (detail set).
            # Poll on each transition, and at most every `_CANCEL_POLL_S` on a
            # chatty stage, so a stop is honoured promptly without a store read
            # per message. Record the request in-memory first so the terminal
            # write preserves it, then unwind via GatherCancelled.
            if detail is None or now - last_cancel_poll >= _CANCEL_POLL_S:
                last_cancel_poll = now
                if _cancel_requested(corpus):
                    status["cancel_requested"] = True
                    raise GatherCancelled(corpus)
            recorder.progress(name, index, total, detail)

        workspace = os.path.join(get_cache_dir(), corpus)
        fenced = False
        try:
            # Inside the try so an unfetchable IMAP mail chunk, which fails the
            # gather before any stage runs (`GatherCacheRestoreError`), is
            # recorded as `failed` like any other gather error.
            lifecycle.prepare_workspace(store, corpus, workspace, recorder.note)

            outcome = pipeline.run_pipeline(
                spec,
                _progress,
                recorder.note,
                # Also ends a silent stage (the pipe check polls this on an idle
                # tick), so a lost lease terminates the child promptly.
                lambda: watch.lost.is_set() or _cancel_requested(corpus),
            )
            if outcome is lifecycle.GatherOutcome.GATHERED:
                lifecycle.publish_workspace(
                    store, corpus, workspace, recorder.note, owner=owner, lease=watch
                )
                status["state"] = "done"
            elif outcome is lifecycle.GatherOutcome.SKIPPED:
                # Nothing was gathered, so the seeded workspace is the published
                # version unchanged: publishing it would upload an identical copy.
                recorder.note("nothing to gather (up to date); nothing published")
                status["state"] = "done"
            else:
                status["state"] = "failed"
                status["error"] = (
                    f"'{corpus}' is not a recognized Working Group / Research "
                    "Group, a known mailing list, a draft/repo set, or a "
                    "synthetic (x-) corpus. Check the spelling or add sources."
                )
        except GatherCancelled:
            if watch.lost.is_set():
                # The checkpoints raise this to unwind a gather whose lease went;
                # that is a failure, not a requested stop.
                fenced = True
                status["state"] = "failed"
                status["error"] = str(lifecycle.lease_lost_error(watch))[:500]
            else:
                # Stop requested: do not publish (the cache is partial); leave it
                # for the next gather to overwrite. A cancelled record is
                # terminal, so liveness never relabels it.
                status["state"] = "cancelled"
                status["cancel_requested"] = True
                status["error"] = None
        except LeaseLost as err:
            fenced = True
            status["state"] = "failed"
            status["error"] = f"{type(err).__name__}: {err}"[:500]
        except BaseException as err:  # pylint: disable=broad-except
            status["state"] = "failed"
            status["error"] = f"{type(err).__name__}: {err}"[:500]
        finally:
            status["finished"] = _now_iso()
            _mark_finishing(corpus)
            # A gather that lost its lease must not overwrite the fleet record
            # of whoever holds the corpus now.
            _write_status(store, status, publish=not fenced)
    finally:
        store.release_gather_slot(slot_owner)
        if run_start is not None:
            serve_metrics.record_gather_finished(
                str(status["state"]), time.monotonic() - run_start
            )


def _fail_lease_lost(
    store: Any, status: Dict[str, Any], watch: lifecycle.LeaseWatch, corpus: str
) -> None:
    """Finalise a queued job whose lease went before it got a slot: failed, and
    recorded locally only (the fleet record belongs to the new holder)."""
    status["state"] = "failed"
    status["error"] = str(lifecycle.lease_lost_error(watch))[:500]
    status["finished"] = _now_iso()
    _mark_finishing(corpus)
    _write_status(store, status, publish=False)


def read_status(corpus: str) -> Optional[Dict[str, Any]]:
    """Read one corpus's gather status, or None if none recorded (or the
    name is not a safe path segment).

    On the **cloud** backend the status comes from the control plane, so a
    `gather_status` call answered by any replica sees a gather queued or running
    on another (G-8); its `queued`/`running`→`interrupted` relabel keys off the
    cross-host *lease* (the cloud topology shares the control plane, not the
    cache, so a local file cannot answer for another host).

    On the **local** backend status is the per-corpus `gather-status.json`, and
    a non-terminal (`queued`/`running`) record is relabelled `interrupted` when
    its writer is gone: a job of this process that is no longer in the live
    registry (the worker is a daemon, so a restart/kill would otherwise leave
    the record stuck), or another process — an `ietf-llm` CLI gather on this
    host — whose recorded pid is dead. The local backend is a single host, so
    the pid is a sound liveness check there, provided the record is also recent
    (a reused pid cannot keep a long-dead gather `running`).
    """
    if not valid_corpus_name(corpus):
        return None
    # Fleet-visible status first (cloud backend); None on the local backend.
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    fleet = corpus_store.get_corpus_store().get_gather_status(corpus)
    if fleet is not None:
        return fleet
    try:
        with open(_status_path(corpus), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        result = dict(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if result.get("state") in ("queued", "running"):
        pid = result.get("pid")
        if isinstance(pid, int) and pid != os.getpid():
            tracked = _pid_alive(pid) and _recently_updated(result)
        else:
            with _registry_lock:
                tracked = corpus in _jobs
        if not tracked:
            result["state"] = "interrupted"
    return result


def _recently_updated(status: Dict[str, Any]) -> bool:
    """Whether a status record was written within the lease TTL. A pid alone
    cannot vouch for a record: after a reboot or long uptime the writer's pid
    may belong to an unrelated process. A record with no readable `updated`
    stamp is taken at the pid's word."""
    stamp = freshness.parse_iso(str(status.get("updated") or ""))
    if stamp is None:
        return True
    return (datetime.now(timezone.utc) - stamp).total_seconds() <= lifecycle.LEASE_TTL


def _pid_alive(pid: int) -> bool:
    """Whether another process on this host is still running. `os.kill(pid, 0)`
    is the POSIX existence probe; on Windows that call would *terminate* the
    process, so there a foreign record is taken at its word."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def local_inflight(corpus: str) -> Optional[Dict[str, Any]]:
    """Cheap, network-free check: is a gather for `corpus` live in *this*
    process right now? Returns its local status dict (state `queued`/`running`)
    or None.

    Unlike `read_status`, this never consults the cloud control plane, so the
    read path can flag an in-flight refresh on every top-level response without
    adding a control-plane round-trip to each call. The in-process job registry
    is the authoritative "live here" signal; a gather running on another host
    (cloud backend) is invisible here — and harmlessly so, since the cloud read
    path serves the last *published* version, not the half-written one. The
    registry membership also means we never mislabel a dead record `interrupted`
    the way `read_status` has to.
    """
    if not valid_corpus_name(corpus):
        return None
    with _registry_lock:
        state = _jobs.get(corpus)
    if state not in ("queued", "running"):
        return None
    try:
        with open(_status_path(corpus), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        result = dict(data)
    except (OSError, json.JSONDecodeError, ValueError):
        # The registry says it is live; surface that even without stage detail.
        return {"corpus": corpus, "state": state}
    if result.get("state") not in ("queued", "running"):
        result["state"] = state
    return result


def has_local_status(corpus: str) -> bool:
    """Cheap, network-free check: did a gather run through *this* runner leave a
    status record for `corpus` on local disk?

    For the read path, which wants to know whether pointing a reader at
    `gather_status` will actually tell them anything. A corpus gathered before
    the CLI wrote status records (or seeded from the public store) has none,
    and `gather_status` would answer "no gather has been recorded", suggesting
    the reader start one. That is a dead end, and for the case this exists to
    explain (an upstream feed that lags its archive) the suggested re-gather
    reads the same feed.

    Deliberately *not* `read_status`: that consults the cloud control plane
    first, so calling it per response would put an S3 round-trip on a read tool
    that is meant to stay offline. The cost of only seeing local records is a
    false negative on the cloud backend — a fleet record written by another
    replica — which merely omits the pointer. Omitting it is the safe direction;
    a dead-end pointer is not.
    """
    return valid_corpus_name(corpus) and os.path.exists(_status_path(corpus))


def all_statuses() -> List[Dict[str, Any]]:
    """Every recorded gather status, newest activity first.

    Merges the control plane's fleet-visible statuses (cloud backend; empty on
    the local backend) with this host's per-corpus cache records, so a
    no-corpus listing also sees gathers queued or running on other replicas,
    not just the corpora cached locally.
    """
    # pylint: disable-next=import-outside-toplevel
    from ..store import corpus as corpus_store

    out: List[Dict[str, Any]] = []
    seen: "set[str]" = set()
    for status in corpus_store.get_corpus_store().list_gather_statuses():
        corpus = status.get("corpus")
        if isinstance(corpus, str) and corpus not in seen:
            seen.add(corpus)
            out.append(status)
    root = get_cache_dir()
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name.startswith((".", "_")) or name in seen:
                continue
            local = read_status(name)
            if local is not None:
                seen.add(name)
                out.append(local)
    out.sort(key=lambda s: str(s.get("updated") or ""), reverse=True)
    return out
