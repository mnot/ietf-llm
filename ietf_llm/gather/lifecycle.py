"""The store-facing gather lifecycle, shared by the `ietf-llm` CLI and the MCP
gather runner.

A gather is more than running the pipeline. On the cloud backend the local
cache is scratch: the gathered tree reaches readers only when it is *published*,
and a corpus another host is already gathering must be refused, not raced. The
steps around the pipeline are the same whoever drives it — take the per-corpus
lease, wait for a fleet gather slot, seed the workspace from the published
version, hydrate the gather accelerator caches, run, publish, persist the
caches, release — and this module owns them. `runner` wraps them with its
queue, status streaming and cooperative cancel for the MCP `start_gather`
tool; `cli.main` runs them synchronously around `sequencer._gather_one`
(`gather_and_publish`). Every store call here is a no-op on the local backend,
so the laptop CLI behaves exactly as before.

The status record (`gather-status.json` locally and, on the cloud backend, the
control plane) is written by both drivers through `StatusRecorder`, so a
cron-driven CLI gather is visible to `gather_status` on any replica, and the
lease it holds makes a concurrent `start_gather` report *already running*.
"""

from __future__ import annotations

import enum
import json
import os
import shutil
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from ..atomicio import atomic_open
from ..config import service as service_config
from ..log import LogLevel, Verbosity, log
from ..paths import INDEX_FILE_NAMES, get_cache_dir, get_index_dir
from ..store.corpus import GatherCacheRestoreError

#: Cross-host gather-lease TTL (cloud backend). The lease is held for the whole
#: gather and renewed periodically (see `LEASE_HEARTBEAT_S`), so even a gather
#: longer than the TTL cannot let it lapse and admit a second writer.
LEASE_TTL = 3600.0

#: How often a heartbeat renews the lease/slot — comfortably under the TTL so a
#: slow renew or a stalled stage still refreshes in time.
LEASE_HEARTBEAT_S = 900.0

#: How often a gather waiting for a fleet slot retries the slot pool.
SLOT_POLL_S = 5.0

#: Minimum seconds between mid-stage detail writes (see `StatusRecorder`). A
#: long stage (the mailing-list download) reports a counter per batch;
#: coalescing to this interval keeps the status file / control plane from
#: churning while still giving a poller fresh movement to show.
DETAIL_MIN_INTERVAL_S = 3.0

STATUS_NAME = "gather-status.json"

#: A random per-process nonce so the lease owner id is unique even if two
#: replicas collide on hostname *and* pid (some orchestrators reuse pod
#: hostnames) — without it, a renew/release could cross hosts.
_PROCESS_NONCE = uuid.uuid4().hex[:8]

NoteFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int, Optional[str]], None]


class GatherOutcome(enum.Enum):
    """What one run of the gather pipeline did. Only `GATHERED` leaves a tree
    worth publishing: a `SKIPPED` run (freshness debounce, reuse hint) left the
    workspace as it was, and publishing it again would upload an identical
    version for nothing; `UNUSABLE` means the corpus name resolved to nothing."""

    UNUSABLE = "unusable"
    SKIPPED = "skipped"
    GATHERED = "gathered"


class GatherBusy(RuntimeError):
    """Another gatherer holds the lease for this corpus (here or, on the cloud
    backend, on another host)."""

    def __init__(self, corpus: str) -> None:
        super().__init__(f"a gather for '{corpus}' is already running")
        self.corpus = corpus


def process_owner() -> str:
    """Identify this gather process for the cross-host lease: host + pid +
    a per-process random nonce."""
    return f"{socket.gethostname()}:{os.getpid()}:{_PROCESS_NONCE}"


def slot_owner(owner: str, corpus: str) -> str:
    """The fleet-slot key for one gather. Per-*job* (process owner + corpus), not
    per-process, so N concurrent gathers on one host hold N distinct slots and
    the fleet cap is counted correctly. A host gathers a given corpus at most
    once at a time (deduped), so this is unique per concurrent gather."""
    return f"{owner}:{corpus}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_path(corpus: str) -> str:
    return os.path.join(get_cache_dir(), corpus, STATUS_NAME)


# --- lease + slot ----------------------------------------------------------


def heartbeat(
    store: Any,
    corpus: str,
    owner: str,
    stop: threading.Event,
    *,
    slot: Optional[str] = None,
    slot_held: Optional[threading.Event] = None,
) -> None:
    """Renew one gather's lease — and its fleet slot once `slot_held` is set —
    periodically, so a gather longer than the TTL cannot let either lapse (which
    would admit a second writer). Exits when `stop` is set or the lease is lost
    (renew returns False). No-op renews on the local backend."""
    while not stop.wait(LEASE_HEARTBEAT_S):
        if not store.renew_lease(corpus, owner, LEASE_TTL):
            break
        if slot is not None and slot_held is not None and slot_held.is_set():
            store.renew_gather_slot(slot, LEASE_TTL)


@contextmanager
def held_gather(
    store: Any,
    corpus: str,
    owner: str,
    *,
    on_wait: Optional[Callable[[], None]] = None,
) -> Iterator[None]:
    """Hold `corpus`'s gather lease and one fleet gather slot for the block.

    Takes the lease first (raising `GatherBusy` if another gatherer holds it),
    starts a heartbeat renewing it, then blocks until a fleet slot is free —
    calling `on_wait` once if it has to wait — and releases both on exit,
    however the block ends. The MCP runner does the same dance but spread over
    enqueue, worker and a shared heartbeat (`runner`); this is the synchronous
    form for a single in-process gather."""
    if not store.acquire_lease(corpus, owner, LEASE_TTL):
        raise GatherBusy(corpus)
    slot = slot_owner(owner, corpus)
    slot_held = threading.Event()
    stop = threading.Event()
    beat = threading.Thread(
        target=heartbeat,
        args=(store, corpus, owner, stop),
        kwargs={"slot": slot, "slot_held": slot_held},
        name=f"gather-heartbeat-{corpus}",
        daemon=True,
    )
    beat.start()
    try:
        max_inflight = service_config.gather_max_inflight()
        waited = False
        while not store.acquire_gather_slot(slot, corpus, LEASE_TTL, max_inflight):
            if not waited and on_wait is not None:
                on_wait()
            waited = True
            time.sleep(SLOT_POLL_S)
        slot_held.set()
        yield
    finally:
        stop.set()
        if slot_held.is_set():
            try:
                store.release_gather_slot(slot)
            except Exception:  # pylint: disable=broad-except
                pass
        try:
            store.release_lease(corpus, owner)
        except Exception:  # pylint: disable=broad-except
            pass


# --- workspace: seed / hydrate before, publish / persist after -------------


def index_extra_files(corpus: str, workspace: str) -> Dict[str, str]:
    """Index files that belong in the published version but live *outside*
    `workspace` — the embeddings index when `IETF_LLM_INDEX_DIR` points away from
    the cache. Returns `{}` when the index dir is inside the workspace (the
    default layout), since the workspace walk already captures it. So a cloud
    reader replica gets the version's `embeddings.db` regardless of where the
    index is configured (G-2).

    Filtered to `paths.INDEX_FILE_NAMES` — the write side of the split-index
    round trip `CloudCorpusStore.seed_workspace` reverses — so the two halves
    name the same set and cannot drift apart again (issue #224): an unrelated
    file that ended up in a split index dir would otherwise be uploaded to the
    version root as if it were corpus content."""
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
        if name in INDEX_FILE_NAMES and os.path.isfile(os.path.join(index_dir, name))
    }


def clear_local_state(corpus: str, workspace: str) -> None:
    """Remove everything a gather of `corpus` would otherwise reuse from this
    host (`--clear-cache`): the workspace, the cached mailing-list mail
    (`imap-cache/<corpus>/`, which sits outside the workspace so that a plain
    re-gather keeps it), this corpus's own conditional-GET store (present only on
    a host that has hydrated one, i.e. the cloud backend; the local backend
    shares one store across corpora, which is left alone), and the embedding
    index when `IETF_LLM_INDEX_DIR` keeps it outside the workspace. Idempotent.

    Mail older than the search window can't be fetched again, so this is not
    reversible for it."""
    # Deferred: `datatracker` pulls in the whole gather source stack, and this
    # module is imported by the serve path.
    # pylint: disable-next=import-outside-toplevel
    from .sources import datatracker

    for path in index_extra_files(corpus, workspace).values():
        os.remove(path)
    for tree in (workspace, os.path.join(get_cache_dir(), "imap-cache", corpus)):
        shutil.rmtree(tree, ignore_errors=True)
    try:
        os.remove(datatracker.http_cache_path(corpus))
    except FileNotFoundError:
        pass


def prepare_workspace(
    store: Any, corpus: str, workspace: str, note: NoteFn, *, seed: bool = True
) -> None:
    """Seed `workspace` from the current published version and restore the
    gather accelerator caches, so an incremental gather on a fresh replica skips
    re-downloading immutable inputs and re-embedding unchanged files, and
    revalidates rather than re-hitting rate-limited upstreams (issue #82). Both
    are no-ops on the local backend and best-effort: a failure means a cold
    gather, not a failed one, and is reported through `note`.

    The one exception is the IMAP mail cache (issue #260). If the store errors
    fetching it, `hydrate_gather_caches` raises `GatherCacheRestoreError`, which
    propagates: publishing without that mail would drop the threads built from
    it (the thread writer deletes any thread file it doesn't regenerate), and a
    retry can succeed. Callers record the gather as failed before any stage runs.

    `seed=False` skips seeding the workspace (a gather that is about to wipe it)."""
    try:
        seeded = store.seed_workspace(corpus, workspace) if seed else None
        if seeded:
            note(f"seeded workspace from published version {seeded}")
    except Exception as err:  # pylint: disable=broad-except
        note(
            f"workspace seed skipped ({type(err).__name__}: {err}); "
            "gathering from scratch"
        )
    try:
        for problem in store.hydrate_gather_caches(corpus):
            note(problem)
    except GatherCacheRestoreError:
        raise
    except Exception as err:  # pylint: disable=broad-except
        note(f"gather-cache hydrate skipped ({type(err).__name__}: {err})")


def publish_workspace(store: Any, corpus: str, workspace: str, note: NoteFn) -> str:
    """Publish the gathered tree as the corpus's new current version and persist
    the accelerator caches for the next cold gather. A no-op finalise on the
    local backend (the cache already is the live version); on the cloud backend
    this uploads the corpus and flips the pointer atomically. The index lives
    outside the cache when `IETF_LLM_INDEX_DIR` is split off, so it is included
    explicitly. Publish failures raise — the gather did not land; the cache
    persist is best-effort and reported through `note`. Returns the version."""
    version = store.publish(
        corpus,
        workspace,
        extra_files=index_extra_files(corpus, workspace) or None,
    )
    try:
        for problem in store.persist_gather_caches(corpus):
            note(problem)
    except Exception as err:  # pylint: disable=broad-except
        note(f"gather-cache persist skipped ({type(err).__name__}: {err})")
    return str(version)


# --- the status record -----------------------------------------------------


def new_status(
    corpus: str, spec: Dict[str, Any], state: str, *, driver: str
) -> Dict[str, Any]:
    """A fresh status record. `spec` is the request as the driver saw it
    (`GatherSpec.to_dict()` for the runner; the CLI's source flags); `driver`
    says which one — `mcp` or `cli` — so an operator can tell a cron gather
    from an in-session one."""
    now = now_iso()
    return {
        "corpus": corpus,
        "state": state,
        "spec": spec,
        "driver": driver,
        "pid": os.getpid(),
        "started": now,
        "updated": now,
        "finished": None,
        "stage": None,
        "stage_index": 0,
        "stage_total": None,
        "stage_detail": None,
        "cancel_token_sha256": None,
        "cancel_requested": False,
        "error": None,
    }


def write_status(store: Any, status: Dict[str, Any]) -> None:
    """Persist a status record locally and publish it to the store so it is
    fleet-visible (G-8). Never fails the gather on a write/publish error."""
    status["updated"] = now_iso()
    corpus = status["corpus"]
    path = status_path(corpus)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with atomic_open(path) as handle:
            json.dump(status, handle, indent=2, sort_keys=True)
    except OSError as err:
        log(
            f"gather: could not write status for {corpus}: {err}",
            Verbosity.STATUS,
            level=LogLevel.ERROR,
        )
    try:
        store.put_gather_status(corpus, status)
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"gather: could not publish status for {corpus}: {err}",
            Verbosity.STATUS,
            level=LogLevel.ERROR,
        )


class StatusRecorder:
    """Keeps one gather's status record current as the pipeline reports
    progress: stage transitions, throttled mid-stage detail, one-line notes and
    the terminal state. `status` is the live dict, shared with the driver so it
    can add its own fields (the runner's cancel token) between writes."""

    def __init__(self, store: Any, status: Dict[str, Any]) -> None:
        self._store = store
        self.status = status
        self._last_detail_write = 0.0

    def write(self) -> None:
        write_status(self._store, self.status)

    def progress(
        self, name: str, index: int, total: int, detail: Optional[str] = None
    ) -> None:
        """Record a stage transition (`detail` None — always written) or a
        mid-stage detail update (throttled to `DETAIL_MIN_INTERVAL_S`, since a
        long stage can report per batch; the next transition resets the detail
        and writes regardless, so a dropped intermediate count is harmless)."""
        self.status["stage"] = name
        self.status["stage_index"] = index
        self.status["stage_total"] = total
        self.status["stage_detail"] = detail
        if detail is not None:
            now = time.monotonic()
            if now - self._last_detail_write < DETAIL_MIN_INTERVAL_S:
                return
            self._last_detail_write = now
        self.write()

    def note(self, message: str) -> None:
        """Surface a pipeline-level note (e.g. which GitHub repos auto-track
        added) so a `gather_status` poller sees it, not only stderr."""
        notes: List[str] = self.status.setdefault("notes", [])
        notes.append(message)
        self.write()

    def finish(self, state: str, error: Optional[str] = None) -> None:
        self.status["state"] = state
        self.status["error"] = error
        self.status["finished"] = now_iso()
        self.write()


# --- the synchronous driver (CLI) -----------------------------------------


def gather_and_publish(
    store: Any,
    corpus: str,
    run: Callable[[ProgressFn, NoteFn], GatherOutcome],
    *,
    spec: Dict[str, Any],
    verbosity: Verbosity,
    fresh: bool = False,
) -> GatherOutcome:
    """Run one gather synchronously through the whole lifecycle: lease and
    slot, seed and hydrate, `run(progress, note)` (the pipeline), then publish
    and persist when it gathered something. Writes the status record
    throughout, so the gather is visible to `gather_status` like an MCP one.

    `fresh` (`--clear-cache`) starts from nothing instead of from the published
    version: the store's cached gather state for the corpus is deleted (under the
    lease, so no other host is writing it) and so is this host's, before anything
    is restored. Either failing fails the gather -- a clear that quietly kept the
    old mail would defeat the point.

    Raises `GatherBusy` when another gatherer holds the corpus; a pipeline or
    publish exception is recorded as `failed` and re-raised — the caller owns
    the exit code. Only `GATHERED` publishes. The caller screens out an unusable
    name or a corpus that is not due *before* calling this
    (`sequencer.preflight`), so neither takes a lease, slot or status record."""
    owner = process_owner()
    workspace = os.path.join(get_cache_dir(), corpus)

    def _waiting() -> None:
        log(
            f"{corpus}: waiting for a fleet gather slot (at most "
            f"{service_config.gather_max_inflight()} gathers run across the "
            "deployment at once)...",
            verbosity,
            level=LogLevel.STATUS,
        )

    with held_gather(store, corpus, owner, on_wait=_waiting):
        recorder = StatusRecorder(
            store, new_status(corpus, spec, "running", driver="cli")
        )
        recorder.write()
        try:
            if fresh:
                store.clear_gather_caches(corpus)
                clear_local_state(corpus, workspace)
            prepare_workspace(store, corpus, workspace, recorder.note, seed=not fresh)
            outcome = run(recorder.progress, recorder.note)
            if outcome is GatherOutcome.GATHERED:
                version = publish_workspace(store, corpus, workspace, recorder.note)
                if service_config.store_backend() == "cloud":
                    log(
                        f"Published {corpus} to the store as version {version}.",
                        verbosity,
                        level=LogLevel.STATUS,
                    )
        except BaseException as err:
            recorder.finish("failed", f"{type(err).__name__}: {err}"[:500])
            raise
        recorder.finish("done")
        return outcome
