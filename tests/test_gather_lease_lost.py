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
    deadline = time.time() + 2.0
    while store.renew_calls < 4 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and not watch.lost.is_set()
    assert store.renew_calls >= 4
    stop.set()
    thread.join(timeout=1.0)


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
    thread.join(timeout=1.0)
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
    deadline = time.time() + 2.0
    while len(slot_calls) < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and len(slot_calls) >= 3
    stop.set()
    thread.join(timeout=1.0)


# --- the CLI driver --------------------------------------------------------


def _cli_run(store: _Store, pipeline: Any) -> GatherOutcome:
    return lifecycle.gather_and_publish(
        store, "tls", pipeline, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET
    )


def test_cli_lease_lost_mid_run_stops_at_the_next_checkpoint_without_publishing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.005)
    store = _Store()
    reached: List[int] = []

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        progress("a", 1, 400, None)
        store.renew_result = False  # the next beat finds the lease gone
        for i in range(2, 400):
            time.sleep(0.005)
            progress("a", i, 400, None)
            reached.append(i)
        return GatherOutcome.GATHERED

    with pytest.raises(LeaseLost, match="stopped without publishing"):
        _cli_run(store, pipeline)
    assert len(reached) < 390  # stopped early, not run to completion
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

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        progress("a", 1, 1, None)
        store.renew_result = False
        time.sleep(0.1)
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


def _wait_terminal(corpus: str, timeout: float = 5.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")


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
        gate.wait(5.0)
        progress("b", 2, 3)
        return GatherOutcome.GATHERED

    monkeypatch.setattr(sequencer, "run_gather", run)
    started = gather_runner.start(gather_runner.GatherSpec(corpus="tls"))
    assert gather_runner.request_stop("tls", started["cancel_token"])["stopped"]
    gate.set()
    assert _wait_terminal("tls")["state"] == "cancelled"
    assert "publish" not in store.calls


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
    deadline = time.time() + 2.0
    while len(beats) < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert thread.is_alive() and len(beats) >= 3
    stop.set()
    thread.join(timeout=1.0)
