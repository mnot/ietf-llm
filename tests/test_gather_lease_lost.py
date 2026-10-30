"""A gather that loses its lease (issue #288) must stop, must not publish, and
must not overwrite the fleet status record of whoever holds the corpus now. The
store-side refusal to flip the pointer is in test_corpus_store_lease.py; this is
the gather side: the heartbeats that notice, and the CLI and MCP drivers that
act on it."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather import lifecycle, runner as gather_runner, sequencer
from ietf_llm.gather.lifecycle import GatherOutcome, LeaseWatch
from ietf_llm.log import Verbosity
from ietf_llm.store.corpus import LeaseLost, LocalCorpusStore


class _Store(LocalCorpusStore):
    """A local store with a scriptable lease, recording what the gather did."""

    def __init__(self) -> None:
        self.renew_result: Any = True  # True / False / an exception to raise
        self.renew_calls = 0
        self.publish_error: Optional[BaseException] = None
        self.calls: List[str] = []
        self.statuses: List[Dict[str, Any]] = []
        self.slot_free = True

    def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        self.renew_calls += 1
        if isinstance(self.renew_result, BaseException):
            raise self.renew_result
        return bool(self.renew_result)

    def release_lease(self, corpus: str, owner: str) -> None:
        self.calls.append("release_lease")

    def acquire_gather_slot(self, owner: str, corpus: str, ttl: float, max_inflight: int) -> bool:
        return self.slot_free

    def publish(self, corpus: str, workspace: str, *, extra_files: Any = None, lease_owner: Optional[str] = None) -> str:
        self.calls.append("publish")
        if self.publish_error is not None:
            raise self.publish_error
        return "v1"

    def release_gather_slot(self, owner: str) -> None:
        self.calls.append("release_slot")

    def seed_workspace(self, corpus: str, dest_root: str) -> Optional[str]:
        self.calls.append("seed")
        return None

    def persist_gather_caches(self, corpus: str) -> List[str]:
        self.calls.append("persist")
        return []

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        self.statuses.append(dict(status))

    def states(self) -> List[str]:
        return [s["state"] for s in self.statuses]


# --- LeaseWatch -----------------------------------------------------------


def test_watch_marks_lost_when_a_renewal_is_refused() -> None:
    store, watch = _Store(), LeaseWatch("tls")
    store.renew_result = True
    watch.renew(store, "me")
    assert not watch.lost.is_set()
    store.renew_result = False
    watch.renew(store, "me")
    assert watch.lost.is_set() and "another gatherer" in watch.reason


def test_watch_tolerates_isolated_errors_but_not_persistent_ones() -> None:
    store, watch = _Store(), LeaseWatch("tls")
    boom = OSError("bucket unreachable")
    # Two errors, a success (which resets the count), two more: never three in a row.
    for result in (boom, boom, True, boom, boom):
        store.renew_result = result
        watch.renew(store, "me")
    assert not watch.lost.is_set()
    store.renew_result = boom  # the third consecutive error
    watch.renew(store, "me")
    assert watch.lost.is_set() and "consecutive" in watch.reason


def test_watch_marks_lost_by_deadline_however_slow_each_failure_was() -> None:
    # Two slow failures (each a long botocore retry) already span three quarters
    # of the TTL: the count of failures is not what protects the lease.
    now = [0.0]
    store, watch = _Store(), LeaseWatch("tls", clock=lambda: now[0])
    boom = OSError("timeout")
    store.renew_result = True
    now[0] = 900.0
    watch.renew(store, "me")  # a good renewal restarts the clock
    store.renew_result = boom
    now[0] = 900.0 + 2000.0
    watch.renew(store, "me")  # first failure, 2000s since the last success
    assert not watch.lost.is_set()
    now[0] = 900.0 + 0.75 * lifecycle.LEASE_TTL
    watch.renew(store, "me")  # second failure, at the deadline
    assert watch.lost.is_set() and "no successful renewal" in watch.reason


def test_watch_deadline_counts_from_acquisition_and_resets_on_success() -> None:
    now = [100.0]
    store, watch = _Store(), LeaseWatch("tls", clock=lambda: now[0])
    boom = OSError("x")
    store.renew_result = boom
    now[0] = 100.0 + 0.75 * lifecycle.LEASE_TTL - 1
    watch.renew(store, "me")
    assert not watch.lost.is_set()  # just inside the deadline
    store.renew_result = True
    watch.renew(store, "me")  # recovers
    store.renew_result = boom
    now[0] += 0.75 * lifecycle.LEASE_TTL - 1
    watch.renew(store, "me")
    assert not watch.lost.is_set()


def test_watch_says_the_record_could_not_be_updated_not_that_another_gatherer_has_it() -> None:
    # A spent CAS-retry budget says nothing about who holds the lease: it counts
    # as a store error, and the reason names the conflict.
    from ietf_llm.store.control import KvControlPlane, LeaseConflict
    from ietf_llm.store.kv import ANY, InMemoryKvStore

    class _Losing(InMemoryKvStore):
        losing = False

        def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
            if self.losing and key.endswith("/lease"):
                return None
            return super().put(key, value, expect=expect)

    kv = _Losing()
    control = KvControlPlane(kv)
    control.acquire_lease("tls", "me", ttl=3600.0)
    kv.losing = True

    class _Conflicted(_Store):
        def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
            return control.renew_lease(corpus, owner, ttl)

    store, watch = _Conflicted(), LeaseWatch("tls")
    for _ in range(lifecycle.LEASE_RENEW_MAX_FAILURES - 1):
        watch.renew(store, "me")
        assert not watch.lost.is_set()  # one conflict is not a lost lease
    watch.renew(store, "me")
    assert watch.lost.is_set()
    assert "could not update the lease record" in watch.reason
    assert "another gatherer" not in watch.reason
    assert control.lease_holder("tls") == "me"
    assert LeaseConflict.__name__ in watch.reason


def test_watch_is_sticky() -> None:
    store, watch = _Store(), LeaseWatch("tls")
    store.renew_result = False
    watch.renew(store, "me")
    calls = store.renew_calls
    store.renew_result = True
    watch.renew(store, "me")  # no renewal attempted, nothing revived
    assert watch.lost.is_set() and store.renew_calls == calls


# --- the CLI heartbeat -----------------------------------------------------


def test_heartbeat_survives_a_store_error_and_keeps_renewing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    store = _Store()
    errors: List[Exception] = [OSError("blip")]

    def renew(corpus: str, owner: str, ttl: float) -> bool:
        store.renew_calls += 1
        if errors:
            raise errors.pop()
        return True

    store.renew_lease = renew  # type: ignore[method-assign]
    watch, stop = LeaseWatch("tls"), threading.Event()
    thread = threading.Thread(
        target=lifecycle.heartbeat, args=(store, "tls", "me", stop), kwargs={"watch": watch}
    )
    thread.start()
    deadline = time.time() + 30.0
    while store.renew_calls < 4 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and not watch.lost.is_set()
    assert store.renew_calls >= 4
    stop.set()
    thread.join(timeout=10.0)


def test_heartbeat_flags_the_watch_and_exits_when_the_lease_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    store, watch = _Store(), LeaseWatch("tls")
    store.renew_result = False
    thread = threading.Thread(
        target=lifecycle.heartbeat,
        args=(store, "tls", "me", threading.Event()),
        kwargs={"watch": watch},
    )
    thread.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive() and watch.lost.is_set()


def test_slot_renewal_error_does_not_end_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    store, watch = _Store(), LeaseWatch("tls")
    slot_calls: List[int] = []

    def renew_slot(owner: str, ttl: float) -> bool:
        slot_calls.append(1)
        raise OSError("slot key unreachable")

    store.renew_gather_slot = renew_slot  # type: ignore[method-assign]
    held, stop = threading.Event(), threading.Event()
    held.set()
    thread = threading.Thread(
        target=lifecycle.heartbeat,
        args=(store, "tls", "me", stop),
        kwargs={"slot": "s", "slot_held": held, "watch": watch},
    )
    thread.start()
    deadline = time.time() + 30.0
    while len(slot_calls) < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and len(slot_calls) >= 3
    stop.set()
    thread.join(timeout=10.0)


# --- the CLI driver --------------------------------------------------------


def _when_lost(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """An event set the moment any LeaseWatch marks its lease lost, so a test can
    wait for the heartbeat on the thing it did rather than sleep and hope."""
    event = threading.Event()
    original = LeaseWatch._lose

    def lose(self: LeaseWatch, reason: str) -> None:
        original(self, reason)
        event.set()

    monkeypatch.setattr(LeaseWatch, "_lose", lose)
    return event


def _cli_run(store: _Store, pipeline: Any) -> GatherOutcome:
    return lifecycle.gather_and_publish(
        store, "tls", pipeline, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET
    )


def test_cli_lease_lost_mid_run_stops_at_the_next_checkpoint_without_publishing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    lost = _when_lost(monkeypatch)
    store = _Store()
    reached: List[int] = []

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        progress("a", 1, 400, None)
        store.renew_result = False  # the next beat finds the lease gone
        assert lost.wait(30.0)  # wait for the beat, however loaded the machine
        progress("a", 2, 400, None)  # the very next checkpoint must stop it
        reached.append(2)
        return GatherOutcome.GATHERED

    with pytest.raises(LeaseLost, match="stopped without publishing"):
        _cli_run(store, pipeline)
    assert reached == []
    assert "publish" not in store.calls and "persist" not in store.calls
    assert store.calls[-1] == "release_lease"
    # Recorded as failed locally, but never put over the new holder's fleet record.
    assert "failed" not in store.states()
    assert '"state": "failed"' in Path(lifecycle.status_path("tls")).read_text()


def test_cli_lease_lost_during_a_silent_stage_refuses_to_publish(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No progress call after the loss (a long silent stage): the publish itself
    # is the last checkpoint.
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    store = _Store()

    lost = _when_lost(monkeypatch)

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        progress("a", 1, 1, None)
        store.renew_result = False
        assert lost.wait(30.0)  # wait for the beat, however loaded the machine
        return GatherOutcome.GATHERED

    with pytest.raises(LeaseLost):
        _cli_run(store, pipeline)
    assert "publish" not in store.calls and "persist" not in store.calls
    assert "failed" not in store.states()


def test_cli_publish_refused_by_the_store_is_fenced_and_skips_the_cache_persist(
    isolated_home: Path,
) -> None:
    store = _Store()
    store.publish_error = LeaseLost("tls", "another gatherer holds it; nothing published")
    with pytest.raises(LeaseLost):
        _cli_run(store, lambda progress, note: GatherOutcome.GATHERED)
    assert "persist" not in store.calls  # the caches are the new holder's now
    assert "failed" not in store.states()
    assert store.calls[-1] == "release_lease"


def test_cli_lease_lost_while_waiting_for_a_slot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    monkeypatch.setattr(lifecycle, "SLOT_POLL_S", 0.01)
    store = _Store()
    store.slot_free = False
    store.renew_result = False
    ran: List[str] = []

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        ran.append("x")
        return GatherOutcome.GATHERED

    with pytest.raises(LeaseLost):
        _cli_run(store, pipeline)
    assert ran == []


def test_cli_normal_gather_is_unaffected(isolated_home: Path) -> None:
    store = _Store()
    outcome = _cli_run(store, lambda progress, note: GatherOutcome.GATHERED)
    assert outcome is GatherOutcome.GATHERED
    assert "publish" in store.calls and store.states()[-1] == "done"


# --- the MCP runner --------------------------------------------------------


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    from ietf_llm.store import corpus as corpus_store

    monkeypatch.setattr(corpus_store, "get_corpus_store", lambda: store)


def _wait_terminal(corpus: str, timeout: float = 30.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


def _wait_idle(corpus: str, timeout: float = 30.0) -> None:
    """Wait until the worker has dropped `corpus` from its registry, i.e. finished
    its cleanup after the terminal status. Waits on the registry condition the
    worker notifies, not on a poll."""
    with gather_runner._registry_cond:
        assert gather_runner._registry_cond.wait_for(
            lambda: corpus not in gather_runner._jobs, timeout=timeout
        ), f"worker for {corpus} never finished its cleanup"


@pytest.mark.usefixtures("gather_in_process")
def test_runner_lease_lost_mid_run_fails_without_publishing_or_clobbering(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)
    reached: List[str] = []

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        progress("a", 1, 3)
        store.renew_result = False
        gather_runner._heartbeat_once(gather_runner._owner())  # what the beat does
        progress("b", 2, 3)  # must raise
        reached.append("b")
        return GatherOutcome.GATHERED

    monkeypatch.setattr(sequencer, "run_gather", run)
    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    status = _wait_terminal("tls")
    assert status["state"] == "failed" and "lost" in status["error"]
    assert not status.get("cancel_requested")
    assert reached == [] and "publish" not in store.calls
    assert "failed" not in store.states()  # the fleet record is the new holder's


@pytest.mark.usefixtures("gather_in_process")
def test_runner_lease_lost_with_no_further_checkpoint_does_not_publish(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        store.renew_result = False
        gather_runner._heartbeat_once(gather_runner._owner())
        return GatherOutcome.GATHERED

    monkeypatch.setattr(sequencer, "run_gather", run)
    gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    status = _wait_terminal("tls")
    assert status["state"] == "failed" and "lost" in status["error"]
    assert "publish" not in store.calls and "persist" not in store.calls
    assert "failed" not in store.states()


@pytest.mark.usefixtures("gather_in_process")
def test_runner_publish_refused_by_the_store_fails_and_skips_persist(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.publish_error = LeaseLost("tls", "taken over during the upload; nothing published")
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(sequencer, "run_gather", lambda *a, **k: GatherOutcome.GATHERED)
    gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    status = _wait_terminal("tls")
    assert status["state"] == "failed" and "LeaseLost" in status["error"]
    assert "persist" not in store.calls and "failed" not in store.states()


@pytest.mark.usefixtures("gather_in_process")
def test_runner_user_cancel_is_still_cancelled_not_lease_lost(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)
    gate = threading.Event()

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        progress("a", 1, 3)
        gate.wait(30.0)
        progress("b", 2, 3)
        return GatherOutcome.GATHERED

    monkeypatch.setattr(sequencer, "run_gather", run)
    started = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert gather_runner.request_stop("tls", started["cancel_token"])["stopped"]
    gate.set()
    assert _wait_terminal("tls")["state"] == "cancelled"
    assert "publish" not in store.calls


@pytest.mark.usefixtures("gather_in_process")
def test_stop_cannot_overwrite_the_terminal_status_the_worker_wrote(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """request_stop used to write back the status record it had read, after
    setting the cancel event. A worker that saw the event and finished first had
    its terminal `cancelled` record replaced by that stale `queued` copy, which
    `read_status` then relabels `interrupted`: a cancel that never reached a
    terminal state. Hold the caller's write until the worker's terminal write has
    landed, which makes the interleaving deterministic."""
    store = _Store()
    _patch_store(monkeypatch, store)
    gate, terminal = threading.Event(), threading.Event()
    real_write = gather_runner._write_status
    caller = threading.current_thread()

    def write(st: Any, status: Dict[str, Any], **kw: Any) -> None:
        stale = status.get("state") in ("queued", "running") and status.get("cancel_requested")
        if threading.current_thread() is caller and stale:
            gate.set()  # let the worker run on to its cancel checkpoint ...
            terminal.wait(10.0)  # ... and finish, before this stale write lands
        real_write(st, status, **kw)
        if status.get("state") in ("done", "failed", "cancelled"):
            terminal.set()

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        progress("a", 1, 3)
        gate.wait(10.0)
        progress("b", 2, 3)
        return GatherOutcome.GATHERED

    monkeypatch.setattr(gather_runner, "_write_status", write)
    monkeypatch.setattr(sequencer, "run_gather", run)
    started = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert gather_runner.request_stop("tls", started["cancel_token"])["stopped"]
    gate.set()
    assert terminal.wait(10.0)
    _wait_idle("tls")
    status = gather_runner.read_status("tls")
    assert status is not None and status["state"] == "cancelled"
    assert status["cancel_requested"] is True


@pytest.mark.usefixtures("gather_in_process")
def test_stop_is_acknowledged_by_gather_status_without_being_written(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop, then ask the tool: the stop must show as pending although the
    request wrote nothing (the worker owns the record), and must not outlive its
    job -- not on the terminal record, and not on the next gather of the same
    corpus. Real runner, real tools, records written by the real worker."""
    import json

    from ietf_llm.mcp import gather as gather_tools

    store = _Store()
    _patch_store(monkeypatch, store)
    holding = [threading.Event(), threading.Event()]  # at the gate, per run
    gates = [threading.Event(), threading.Event()]
    runs: List[int] = []

    def run(argv: List[str], verbosity: Any, progress: Any = None, note_fn: Any = None) -> GatherOutcome:
        n = len(runs)
        runs.append(1)
        progress("a", 1, 3)
        holding[n].set()
        gates[n].wait(30.0)
        progress("b", 2, 3)
        return GatherOutcome.GATHERED

    monkeypatch.setattr(sequencer, "run_gather", run)
    started = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert holding[0].wait(30.0)
    assert "stop requested" not in gather_tools.tool_gather_status("tls")

    assert "Requested stop" in gather_tools.tool_stop_gather("tls", started["cancel_token"])
    shown = gather_tools.tool_gather_status("tls")
    assert "running" in shown and "stop requested" in shown
    # An overlay on read: nothing was written for the request itself.
    with open(lifecycle.status_path("tls"), encoding="utf-8") as handle:
        assert not json.load(handle).get("cancel_requested")

    gates[0].set()
    assert _wait_terminal("tls")["state"] == "cancelled"
    _wait_idle("tls")
    assert "stop requested" not in gather_tools.tool_gather_status("tls")

    # The next gather of the same corpus starts clean.
    again = gather_runner.start(gather_runner.GatherSpec(corpus="tls", force=True))
    assert again["started"], again
    assert holding[1].wait(30.0)
    shown = gather_tools.tool_gather_status("tls")
    assert "running" in shown and "stop requested" not in shown
    gates[1].set()
    _wait_idle("tls")


def test_stop_for_a_job_on_another_host_writes_the_flag_only_to_a_live_record(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No local event: the published flag is the only channel. It is written
    over the record as it is now, and not at all once that has gone terminal."""

    class _Fleet(_Store):
        def __init__(self, reads: List[str]) -> None:
            super().__init__()
            self.reads = reads

        def get_gather_status(self, corpus: str) -> Optional[Dict[str, Any]]:
            state = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            return {
                "corpus": corpus,
                "state": state,
                "cancel_token_sha256": gather_runner._hash_token("tok"),
            }

    live = _Fleet(["running"])
    _patch_store(monkeypatch, live)
    assert gather_runner.request_stop("tls", "tok")["stopped"]
    assert [s["cancel_requested"] for s in live.statuses] == [True]

    # The job finishes between the check and the write: leave the record alone.
    racing = _Fleet(["running", "cancelled"])
    _patch_store(monkeypatch, racing)
    assert gather_runner.request_stop("tls", "tok")["stopped"]
    assert racing.statuses == []


@pytest.mark.usefixtures("gather_in_process")
def test_runner_lease_lost_while_queued_for_a_slot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    store.slot_free = False
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(lifecycle, "SLOT_POLL_S", 0.01)

    def never(*a: Any, **k: Any) -> GatherOutcome:
        raise AssertionError("a gather without a slot must not run")

    monkeypatch.setattr(sequencer, "run_gather", never)
    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    store.renew_result = False
    gather_runner._heartbeat_once(gather_runner._owner())
    status = _wait_terminal("tls")
    assert status["state"] == "failed" and "lost" in status["error"]
    assert "failed" not in store.states()


@pytest.mark.usefixtures("gather_in_process")
def test_runner_lease_lost_while_queued_with_a_free_slot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease went while the job sat in the queue and a slot is free when the
    worker reaches it, so the slot-wait loop is skipped: the job must still not
    write `running` over the new holder's fleet record, seed, or start a child."""
    store = _Store()
    store.slot_free = True
    _patch_store(monkeypatch, store)

    def never(*a: Any, **k: Any) -> GatherOutcome:
        raise AssertionError("a gather that lost its lease must not run")

    monkeypatch.setattr(sequencer, "run_gather", never)
    watch = LeaseWatch("tls")
    store.renew_result = False
    watch.renew(store, "me")
    assert watch.lost.is_set()
    with gather_runner._registry_lock:
        gather_runner._lease_watches["tls"] = watch
    try:
        gather_runner._run_one(store, "me", gather_runner.GatherSpec(corpus="tls"))
    finally:
        with gather_runner._registry_lock:
            gather_runner._lease_watches.pop("tls", None)
    assert store.statuses == []  # nothing, not even `running`, reached the fleet
    assert "seed" not in store.calls and "publish" not in store.calls
    assert store.calls.count("release_slot") == 1
    status = gather_runner.read_status("tls")
    assert status is not None and status["state"] == "failed" and "lost" in status["error"]


def test_heartbeat_marks_only_the_job_whose_lease_went(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _Store()
    _patch_store(monkeypatch, store)
    seen: List[str] = []

    def renew(corpus: str, owner: str, ttl: float) -> bool:
        seen.append(corpus)
        if corpus == "aa":
            return False
        if corpus == "bb":
            raise OSError("one blip")
        return True

    store.renew_lease = renew  # type: ignore[method-assign]
    watches = {name: LeaseWatch(name) for name in ("aa", "bb", "cc", "dd")}
    with gather_runner._registry_lock:
        for name, watch in watches.items():
            gather_runner._jobs[name] = "finishing" if name == "dd" else "running"
            gather_runner._lease_watches[name] = watch
    try:
        gather_runner._heartbeat_once("me")
    finally:
        with gather_runner._registry_lock:
            for name in watches:
                gather_runner._jobs.pop(name, None)
                gather_runner._lease_watches.pop(name, None)
    assert watches["aa"].lost.is_set()
    assert not watches["bb"].lost.is_set()  # a single error is not a lost lease
    assert not watches["cc"].lost.is_set()
    assert "dd" not in seen  # a finishing job's lease is its worker's to release


def test_runner_heartbeat_loop_survives_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    beats: List[int] = []

    def once(owner: str) -> None:
        beats.append(1)
        if len(beats) == 1:
            raise OSError("control plane blip")

    monkeypatch.setattr(gather_runner, "_heartbeat_once", once)
    stop = threading.Event()
    thread = threading.Thread(target=gather_runner._heartbeat_loop, args=("me", stop))
    thread.start()
    deadline = time.time() + 30.0
    while len(beats) < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and len(beats) >= 3
    stop.set()
    thread.join(timeout=10.0)
