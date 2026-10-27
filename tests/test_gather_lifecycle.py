"""Tests for the shared gather lifecycle (ietf_llm.gather.lifecycle) and the
`ietf-llm` CLI's use of it: lease and slot held for the run and released on
every exit, publish only after a real gather, status recorded like an MCP
gather, and the exit code reflecting what happened. The cloud-backend end to
end (CLI gather -> published version -> served) lives in test_cloud_gather.py.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.cli import main as cli_main
from ietf_llm.gather import lifecycle, runner as gather_runner
from ietf_llm.gather.lifecycle import GatherBusy, GatherOutcome
from ietf_llm.log import Verbosity
from ietf_llm.store.corpus import LocalCorpusStore


class _SpyStore(LocalCorpusStore):
    """A local store that records the lifecycle calls made against it and can
    refuse the lease or stall the slot."""

    def __init__(self, *, lease: bool = True, slot_refusals: int = 0) -> None:
        self.calls: List[str] = []
        self.lease = lease
        self.slot_refusals = slot_refusals
        self.statuses: List[Dict[str, Any]] = []

    def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        self.calls.append("acquire_lease")
        return self.lease

    def release_lease(self, corpus: str, owner: str) -> None:
        self.calls.append("release_lease")

    def acquire_gather_slot(self, owner: str, corpus: str, ttl: float, max_inflight: int) -> bool:
        self.calls.append("acquire_slot")
        if self.slot_refusals:
            self.slot_refusals -= 1
            return False
        return True

    def release_gather_slot(self, owner: str) -> None:
        self.calls.append("release_slot")

    def seed_workspace(self, corpus: str, dest_root: str) -> Optional[str]:
        self.calls.append("seed")
        return None

    def hydrate_gather_caches(self, corpus: str) -> None:
        self.calls.append("hydrate")

    def publish(self, corpus: str, workspace: str, *, extra_files: Any = None) -> str:
        self.calls.append("publish")
        return "v1"

    def persist_gather_caches(self, corpus: str) -> None:
        self.calls.append("persist")

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        self.statuses.append(dict(status))


def _run(store: _SpyStore, outcome: GatherOutcome, **kw: Any) -> GatherOutcome:
    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        progress("drafts", 1, 2, None)
        note("hello")
        return outcome

    return lifecycle.gather_and_publish(
        store, "tls", pipeline, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET, **kw
    )


# --- gather_and_publish ---------------------------------------------------


def test_gathered_runs_the_whole_lifecycle_in_order(isolated_home: Path) -> None:
    store = _SpyStore()
    assert _run(store, GatherOutcome.GATHERED) is GatherOutcome.GATHERED
    assert store.calls == [
        "acquire_lease", "acquire_slot", "seed", "hydrate",
        "publish", "persist", "release_slot", "release_lease",
    ]
    final = store.statuses[-1]
    assert final["state"] == "done" and final["driver"] == "cli"
    assert final["stage"] == "drafts" and final["notes"] == ["hello"]
    # The same record is on disk, where the local gather_status reads it.
    on_disk = json.loads(Path(lifecycle.status_path("tls")).read_text())
    assert on_disk["state"] == "done" and on_disk["pid"] == os.getpid()


def test_skipped_does_not_publish(isolated_home: Path) -> None:
    # A debounced run left the workspace as it was: publishing it again would
    # upload an identical version for nothing.
    store = _SpyStore()
    assert _run(store, GatherOutcome.SKIPPED) is GatherOutcome.SKIPPED
    assert "publish" not in store.calls and "persist" not in store.calls
    assert store.calls[-2:] == ["release_slot", "release_lease"]
    assert store.statuses[-1]["state"] == "done"


def test_unusable_records_failed_and_does_not_publish(isolated_home: Path) -> None:
    store = _SpyStore()
    assert _run(store, GatherOutcome.UNUSABLE) is GatherOutcome.UNUSABLE
    assert "publish" not in store.calls
    final = store.statuses[-1]
    assert final["state"] == "failed" and "not a recognized" in final["error"]


def test_pipeline_error_records_failed_releases_and_reraises(isolated_home: Path) -> None:
    store = _SpyStore()

    def boom(progress: Any, note: Any) -> GatherOutcome:
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError, match="upstream down"):
        lifecycle.gather_and_publish(
            store, "tls", boom, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET
        )
    assert "publish" not in store.calls
    assert store.calls[-2:] == ["release_slot", "release_lease"]
    final = store.statuses[-1]
    assert final["state"] == "failed" and "RuntimeError: upstream down" in final["error"]


def test_publish_error_records_failed_and_reraises(isolated_home: Path) -> None:
    class _Broken(_SpyStore):
        def publish(self, corpus: str, workspace: str, *, extra_files: Any = None) -> str:
            raise OSError("bucket gone")

    store = _Broken()
    with pytest.raises(OSError, match="bucket gone"):
        _run(store, GatherOutcome.GATHERED)
    assert store.statuses[-1]["state"] == "failed"
    assert store.calls[-2:] == ["release_slot", "release_lease"]


def test_lease_held_elsewhere_raises_busy_without_running(isolated_home: Path) -> None:
    store = _SpyStore(lease=False)
    ran = False

    def pipeline(progress: Any, note: Any) -> GatherOutcome:
        nonlocal ran
        ran = True
        return GatherOutcome.GATHERED

    with pytest.raises(GatherBusy):
        lifecycle.gather_and_publish(
            store, "tls", pipeline, spec={"corpus": "tls"}, verbosity=Verbosity.QUIET
        )
    assert not ran
    # Nothing to release: the lease was never ours, and no slot was taken.
    assert store.calls == ["acquire_lease"]
    assert store.statuses == []


def test_waits_for_a_fleet_slot_then_proceeds(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "SLOT_POLL_S", 0.01)
    store = _SpyStore(slot_refusals=2)
    assert _run(store, GatherOutcome.GATHERED) is GatherOutcome.GATHERED
    assert store.calls.count("acquire_slot") == 3
    assert store.calls.index("seed") > store.calls.index("acquire_slot")


# --- held_gather + heartbeat ----------------------------------------------


def test_heartbeat_renews_slot_only_once_held(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "LEASE_HEARTBEAT_S", 0.01)
    calls: List[str] = []

    class _Store:
        def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
            calls.append("lease")
            return True

        def renew_gather_slot(self, owner: str, ttl: float) -> bool:
            calls.append("slot")
            return True

    stop = threading.Event()
    held = threading.Event()
    thread = threading.Thread(
        target=lifecycle.heartbeat,
        args=(_Store(), "tls", "me", stop),
        kwargs={"slot": "me:tls", "slot_held": held},
    )
    thread.start()
    deadline = threading.Event()
    deadline.wait(0.05)
    before = list(calls)
    held.set()
    deadline.wait(0.05)
    stop.set()
    thread.join(timeout=1.0)
    assert "lease" in before and "slot" not in before
    assert "slot" in calls


# --- read_status liveness for a foreign (CLI) record ----------------------


def _write_foreign_running(corpus: str, pid: int) -> None:
    status = lifecycle.new_status(corpus, {"corpus": corpus}, "running", driver="cli")
    status["pid"] = pid
    path = Path(lifecycle.status_path(corpus))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status))


def test_read_status_keeps_a_live_cli_gather_running(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_foreign_running("tls", 4242)
    monkeypatch.setattr(gather_runner, "_pid_alive", lambda pid: pid == 4242)
    assert gather_runner.read_status("tls")["state"] == "running"


def test_read_status_relabels_a_dead_cli_gather_interrupted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_foreign_running("tls", 4242)
    monkeypatch.setattr(gather_runner, "_pid_alive", lambda pid: False)
    assert gather_runner.read_status("tls")["state"] == "interrupted"


def test_read_status_own_pid_still_keys_off_the_registry(isolated_home: Path) -> None:
    # A record from this process that is no longer in the job registry is a
    # dead job, however alive the process is.
    _write_foreign_running("tls", os.getpid())
    assert gather_runner.read_status("tls")["state"] == "interrupted"


# --- the CLI: exit codes and the wrapped call ------------------------------


def _quiet_housekeeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "ensure_rfc_index", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "ensure_rfc_corpus", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "ensure_catalog_index", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "ensure_groups_index", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "sync_if_pristine", lambda *a, **k: None)


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    monkeypatch.setattr(cli_main, "get_corpus_store", lambda: store)


def _fake_gather_one(outcome: GatherOutcome) -> Any:
    def fake(args: argparse.Namespace, _v: Any, **kw: Any) -> GatherOutcome:
        return outcome

    return fake


def test_cli_single_gather_publishes_and_exits_zero(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_housekeeping(monkeypatch)
    store = _SpyStore()
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(cli_main, "_gather_one", _fake_gather_one(GatherOutcome.GATHERED))
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls", "--github", "x/y"])
    cli_main.main()
    assert "publish" in store.calls
    spec = store.statuses[-1]["spec"]
    assert spec["corpus"] == "tls" and spec["github"] == ["x/y"]


def test_cli_unusable_name_exits_nonzero(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_housekeeping(monkeypatch)
    _patch_store(monkeypatch, _SpyStore())
    monkeypatch.setattr(cli_main, "_gather_one", _fake_gather_one(GatherOutcome.UNUSABLE))
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "nosuchwg"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1


def test_cli_busy_corpus_exits_nonzero_without_gathering(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_housekeeping(monkeypatch)
    _patch_store(monkeypatch, _SpyStore(lease=False))
    monkeypatch.setattr(
        cli_main,
        "_gather_one",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not gather")),
    )
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "tls"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1


def test_cli_all_skips_busy_and_continues(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sweep: a corpus another gatherer holds is left to it (not a failure),
    # the rest are gathered, and the run exits zero.
    _quiet_housekeeping(monkeypatch)
    monkeypatch.setattr(cli_main.cli_list, "all_corpora", lambda: ["aa", "bb"])

    class _BusyAA(_SpyStore):
        def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
            return corpus != "aa"

    store = _BusyAA()
    _patch_store(monkeypatch, store)
    seen: List[str] = []

    def fake(args: argparse.Namespace, _v: Any, **kw: Any) -> GatherOutcome:
        seen.append(args.wg)
        return GatherOutcome.GATHERED

    monkeypatch.setattr(cli_main, "_gather_one", fake)
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "--all"])
    cli_main.main()
    assert seen == ["bb"]
    assert store.calls.count("publish") == 1


def test_cli_all_unusable_exits_nonzero_after_the_sweep(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_housekeeping(monkeypatch)
    monkeypatch.setattr(cli_main.cli_list, "all_corpora", lambda: ["aa", "bb"])
    _patch_store(monkeypatch, _SpyStore())
    seen: List[str] = []

    def fake(args: argparse.Namespace, _v: Any, **kw: Any) -> GatherOutcome:
        seen.append(args.wg)
        return GatherOutcome.UNUSABLE if args.wg == "aa" else GatherOutcome.GATHERED

    monkeypatch.setattr(cli_main, "_gather_one", fake)
    monkeypatch.setattr(cli_main.sys, "argv", ["ietf-llm", "--all"])
    with pytest.raises(SystemExit) as exc:
        cli_main.main()
    assert exc.value.code == 1
    assert seen == ["aa", "bb"]  # the sweep finished before exiting
