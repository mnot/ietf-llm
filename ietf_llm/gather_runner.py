"""In-process background gather runner for the MCP server.

The MCP server is otherwise read-only and never touches the network; this
module is the one deliberate exception, gated behind
`IETF_LLM_ENABLE_GATHER=1` (see `mcp_server._gather_enabled`). A `start_gather`
tool call returns immediately, enqueuing the request; a background worker runs
the same pipeline as the `ietf-llm` CLI and records stage-level progress to a
per-corpus status record (`~/.cache/ietf-llm/<corpus>/gather-status.json` and,
on the cloud backend, the control plane) that the `gather_status` tool reads
back.

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

import json
import os
import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import canonical, freshness, service_config
from .utils import (
    LogLevel,
    Verbosity,
    atomic_open,
    get_cache_dir,
    get_index_dir,
    log,
)

_STATUS_NAME = "gather-status.json"

#: Cross-host gather-lease TTL (cloud backend). The lease is held for the whole
#: gather and renewed periodically (see `_LEASE_HEARTBEAT_S`), so even a gather
#: longer than the TTL cannot let it lapse and admit a second writer.
_LEASE_TTL = 3600.0

#: How often the heartbeat thread renews the lease/slot — comfortably under the
#: TTL so a slow renew or a stalled stage still refreshes in time.
_LEASE_HEARTBEAT_S = 900.0

#: How often the worker retries the global slot pool while a job sits queued
#: waiting for the fleet to free a slot.
_SLOT_POLL_S = 5.0

#: Minimum seconds between mid-stage detail writes (see `_progress`). A long
#: stage (the mailing-list download) reports a counter per batch; coalescing to
#: this interval keeps the status file / control plane from churning while still
#: giving a poller fresh movement to show.
_DETAIL_MIN_INTERVAL_S = 3.0

#: Default bound on this host's pending-gather backlog (queued + running);
#: overflow is refused so a runaway caller cannot pile up unbounded work.
_DEFAULT_QUEUE_MAX = 16

# A corpus name becomes a cache directory, so it must be a single safe path
# segment: letters/digits/`.`/`-`/`_`, starting with an alphanumeric. This
# bars path separators, `..`, leading dots/dashes, and whitespace — the
# write-side mirror of the read-side `_safe_path` guard, applied *before*
# any path is built. Real corpus names (WG shortnames, list names, `x-`
# synthetics) all satisfy it; unusual custom labels still go via the CLI.
_VALID_CORPUS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_CORPUS_LEN = 128

# This host runs up to N gathers at once (the per-host cap, N =
# service_config.gather_max_inflight()): a pool of N worker threads drains a
# shared FIFO queue, and each running gather holds a per-job global slot in the
# control plane (the fleet-wide cap — N slots total, so the whole deployment
# never exceeds N either). `_jobs` maps corpus -> state ("queued" | "running")
# for in-process dedup, queue-bound enforcement, and local-backend liveness.
_queue: "queue.Queue[GatherSpec]" = queue.Queue()
_jobs: Dict[str, str] = {}
_registry_lock = threading.Lock()
#: The worker pool, mutated in place so it needs no module-level `global` rebind.
_workers: "List[threading.Thread]" = []
_heartbeat_stop = threading.Event()


#: A random per-process nonce so the lease owner id is unique even if two
#: replicas collide on hostname *and* pid (some orchestrators reuse pod
#: hostnames) — without it, a renew/release could cross hosts.
_PROCESS_NONCE = uuid.uuid4().hex[:8]


def _owner() -> str:
    """Identify this gather process for the cross-host lease: host + pid +
    a per-process random nonce."""
    return f"{socket.gethostname()}:{os.getpid()}:{_PROCESS_NONCE}"


def _heartbeat_lease(
    store: Any, corpus: str, owner: str, stop: threading.Event
) -> None:
    """Renew the gather lease periodically so a gather longer than the lease TTL
    cannot let the lease lapse (which would admit a second writer). Exits when
    `stop` is set or if the lease is lost (renew returns False). A no-op renew on
    the local backend, so this thread is harmless there."""
    while not stop.wait(_LEASE_HEARTBEAT_S):
        if not store.renew_lease(corpus, owner, _LEASE_TTL):
            break


def _index_extra_files(corpus: str, workspace: str) -> Dict[str, str]:
    """Index files that belong in the published version but live *outside*
    `workspace` — the embeddings index when `IETF_LLM_INDEX_DIR` points away from
    the cache. Returns `{}` when the index dir is inside the workspace (the
    default layout), since the workspace walk already captures it. So a cloud
    reader replica gets the version's `embeddings.db` regardless of where the
    index is configured (G-2)."""
    index_dir = os.path.join(get_index_dir(), corpus)
    if not os.path.isdir(index_dir):
        return {}
    ws_real = os.path.realpath(workspace)
    idx_real = os.path.realpath(index_dir)
    if idx_real == ws_real or idx_real.startswith(ws_real + os.sep):
        return {}  # inside the workspace; the walk already captures it
    return {
        name: os.path.join(index_dir, name)
        for name in os.listdir(index_dir)
        if os.path.isfile(os.path.join(index_dir, name))
    }


def valid_corpus_name(name: str) -> bool:
    """True if `name` is a safe single path segment usable as a cache dir."""
    return (
        bool(name) and len(name) <= _MAX_CORPUS_LEN and bool(_VALID_CORPUS.match(name))
    )


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
        """Render as CLI-style argv for `__main__.run_gather`."""
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_path(corpus: str) -> str:
    return os.path.join(get_cache_dir(), corpus, _STATUS_NAME)


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
    from . import corpus_store  # pylint: disable=import-outside-toplevel

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
        detail = freshness.debounce_reason(corpus)
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


def start(spec: GatherSpec) -> Dict[str, Any]:
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
    from . import corpus_store  # pylint: disable=import-outside-toplevel

    store = corpus_store.get_corpus_store()
    owner = _owner()
    with _registry_lock:
        if corpus in _jobs:
            return {"started": False, "reason": "already running", "corpus": corpus}
        if len(_jobs) >= _queue_max():
            return {"started": False, "reason": "queue full", "corpus": corpus}
        # Take the per-corpus lease NOW, before queuing: on the cloud backend
        # this atomically dedupes the same corpus across hosts (the loser of a
        # near-simultaneous race is told "already running"), and it is the
        # liveness anchor for the queued state. A no-op grant on the local
        # backend, where the in-process registry above is the dedup.
        if not store.acquire_lease(corpus, owner, _LEASE_TTL):
            return {"started": False, "reason": "already running", "corpus": corpus}
        ahead = len(_jobs)
        _jobs[corpus] = "queued"
    # Publish `queued` BEFORE enqueueing, then enqueue, then ensure the worker is
    # up. Ordering matters: the worker only ever writes `running`/terminal, and
    # it can only dequeue after the put below — so this `queued` write always
    # happens-before the worker's writes and can never clobber a later state
    # (which, with a fast gather, would otherwise leave the record stuck at
    # `queued`). It also makes the queued state fleet-visible at once (a client
    # checking status on another replica sees it, not just the enqueuing host).
    _write_status(store, _new_status(spec, "queued"))
    with _registry_lock:
        _queue.put(spec)
        _ensure_worker(owner)
    # How many gathers this one actually waits behind: with N running
    # concurrently, a free slot exists until `ahead` reaches N, so anything under
    # that starts at once (wait 0). Reported so the tool can say "started" vs
    # "queued behind k" truthfully.
    wait_for = max(0, ahead - service_config.gather_max_inflight() + 1)
    return {"started": True, "queued_behind": wait_for, "corpus": corpus}


def _new_status(spec: GatherSpec, state: str) -> Dict[str, Any]:
    now = _now_iso()
    return {
        "corpus": spec.corpus,
        "state": state,
        "spec": spec.to_dict(),
        "pid": os.getpid(),
        "started": now,
        "updated": now,
        "finished": None,
        "stage": None,
        "stage_index": 0,
        "stage_total": None,
        "stage_detail": None,
        "error": None,
    }


def _write_status(store: Any, status: Dict[str, Any]) -> None:
    """Persist a status record locally and publish it to the store so it is
    fleet-visible (G-8). Never fails the gather on a write/publish error."""
    status["updated"] = _now_iso()
    corpus = status["corpus"]
    path = _status_path(corpus)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with atomic_open(path) as handle:
            json.dump(status, handle, indent=2, sort_keys=True)
    except OSError as err:
        log(
            f"gather_runner: could not write status for {corpus}: {err}",
            Verbosity.STATUS,
            level=LogLevel.ERROR,
        )
    try:
        store.put_gather_status(corpus, status)
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"gather_runner: could not publish status for {corpus}: {err}",
            Verbosity.STATUS,
            level=LogLevel.ERROR,
        )


def _store() -> Any:
    """The current CorpusStore. Fetched fresh (not captured) so the long-lived
    worker/heartbeat always honour the live config rather than whatever was set
    when they first started."""
    from . import corpus_store  # pylint: disable=import-outside-toplevel

    return corpus_store.get_corpus_store()


def _slot_owner(owner: str, corpus: str) -> str:
    """The fleet-slot key for one gather. Per-*job* (process owner + corpus), not
    per-process, so N concurrent gathers on one host hold N distinct slots and
    the fleet cap is counted correctly. A host gathers a given corpus at most
    once at a time (deduped), so this is unique per concurrent gather."""
    return f"{owner}:{corpus}"


def _ensure_worker(owner: str) -> None:
    """Ensure the gather worker pool is at least `gather_max_inflight()` strong,
    and a lease/slot heartbeat is running. Caller holds `_registry_lock`. All are
    daemons that live for the process. Grow-only: it never shrinks the pool (you
    cannot un-start a thread), so in steady state — a fixed N — it grows once and
    returns early thereafter; the fleet slot is the authoritative concurrency
    cap, the pool just needs to be big enough not to be the bottleneck."""
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
    threading.Thread(
        target=_heartbeat_loop,
        args=(owner, _heartbeat_stop),
        name="gather-heartbeat",
        daemon=True,
    ).start()


def _heartbeat_loop(owner: str, stop: threading.Event) -> None:
    """Renew every lease this host holds (queued + running jobs) and each running
    gather's slot, comfortably within the TTL, so a long gather — or a long wait
    for a fleet slot — cannot let the lease/slot lapse. No-ops on the local
    backend. Lives for the process (daemon); `stop` is a test seam."""
    while not stop.wait(_LEASE_HEARTBEAT_S):
        with _registry_lock:
            items = list(_jobs.items())
        if not items:
            continue
        store = _store()
        for corpus, state in items:
            store.renew_lease(corpus, owner, _LEASE_TTL)
            if state == "running":
                store.renew_gather_slot(_slot_owner(owner, corpus), _LEASE_TTL)


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
                f"gather_runner: worker error on {corpus}: {err}",
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
            _queue.task_done()


def _run_one(store: Any, owner: str, spec: GatherSpec) -> None:
    """Run one queued gather: wait for a fleet-wide slot, then run the pipeline,
    publishing status at each transition. Holds the per-corpus lease throughout
    (released by the worker loop). Never raises — failures become a `failed`
    status."""
    # Imported lazily so the read-only serve path doesn't pull the gather
    # pipeline (and its many imports) unless gather is actually enabled.
    from . import __main__ as gather_main  # pylint: disable=import-outside-toplevel

    corpus = spec.corpus
    slot_owner = _slot_owner(owner, corpus)
    status = _new_status(spec, "queued")
    # Wait for one of the fleet-wide gather slots. While queued, the heartbeat
    # renews the lease; re-publishing `queued` keeps `updated` fresh so a stale
    # record is distinguishable. On shutdown, bail without a terminal status —
    # the released lease lets liveness relabel the record `interrupted`.
    max_inflight = service_config.gather_max_inflight()
    while not store.acquire_gather_slot(slot_owner, corpus, _LEASE_TTL, max_inflight):
        if _heartbeat_stop.is_set():
            return
        _write_status(store, status)
        time.sleep(_SLOT_POLL_S)
    try:
        with _registry_lock:
            _jobs[corpus] = "running"
        status["state"] = "running"
        _write_status(store, status)

        last_detail_write = 0.0

        def _progress(
            name: str, index: int, total: int, detail: Optional[str] = None
        ) -> None:
            nonlocal last_detail_write
            status["stage"] = name
            status["stage_index"] = index
            status["stage_total"] = total
            status["stage_detail"] = detail
            # A stage transition (detail is None) always persists. Mid-stage
            # detail updates can fire often on a long stage, so throttle the
            # write/publish to keep a chatty stage from flooding the control
            # plane — the next transition resets the detail to None and writes
            # regardless, so a dropped intermediate count is harmless.
            if detail is not None:
                now = time.monotonic()
                if now - last_detail_write < _DETAIL_MIN_INTERVAL_S:
                    return
                last_detail_write = now
            _write_status(store, status)

        try:
            ok = gather_main.run_gather(
                spec.to_argv(), Verbosity.STATUS, progress=_progress
            )
            if ok:
                # Publish the gathered tree as a new version. A no-op finalise on
                # the local backend (the cache already is the live version); on
                # the cloud backend this uploads the corpus and flips the pointer
                # atomically. The index lives outside the cache when
                # IETF_LLM_INDEX_DIR is split off, so include it explicitly (G-2).
                workspace = os.path.join(get_cache_dir(), corpus)
                store.publish(
                    corpus,
                    workspace,
                    extra_files=_index_extra_files(corpus, workspace) or None,
                )
                status["state"] = "done"
            else:
                status["state"] = "failed"
                status["error"] = (
                    f"'{corpus}' is not a recognized Working Group / Research "
                    "Group, a known mailing list, a draft/repo set, or a "
                    "synthetic (x-) corpus. Check the spelling or add sources."
                )
        except BaseException as err:  # pylint: disable=broad-except
            status["state"] = "failed"
            status["error"] = f"{type(err).__name__}: {err}"[:500]
        finally:
            status["finished"] = _now_iso()
            _write_status(store, status)
    finally:
        store.release_gather_slot(slot_owner)


def read_status(corpus: str) -> Optional[Dict[str, Any]]:
    """Read one corpus's gather status, or None if none recorded (or the
    name is not a safe path segment).

    On the **cloud** backend the status comes from the control plane, so a
    `gather_status` call answered by any replica sees a gather queued or running
    on another (G-8); its `queued`/`running`→`interrupted` relabel keys off the
    cross-host *lease* (the cloud topology shares the control plane, not the
    cache, so a local file cannot answer for another host).

    On the **local** backend status is the per-corpus `gather-status.json`, and
    a non-terminal (`queued`/`running`) record whose corpus is not in this
    process's live job registry is relabelled `interrupted`: the worker is a
    daemon, so a restart/kill would otherwise leave the record stuck. The local
    backend has a single gatherer (this process), so "not tracked here" means
    the job that wrote the record is gone.
    """
    if not valid_corpus_name(corpus):
        return None
    # Fleet-visible status first (cloud backend); None on the local backend.
    from . import corpus_store  # pylint: disable=import-outside-toplevel

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
        with _registry_lock:
            tracked = corpus in _jobs
        if not tracked:
            result["state"] = "interrupted"
    return result


def all_statuses() -> List[Dict[str, Any]]:
    """Every recorded gather status, newest activity first."""
    root = get_cache_dir()
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name.startswith((".", "_")):
            continue
        status = read_status(name)
        if status is not None:
            out.append(status)
    out.sort(key=lambda s: str(s.get("updated") or ""), reverse=True)
    return out
