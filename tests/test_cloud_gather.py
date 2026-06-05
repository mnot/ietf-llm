"""Feature 6 capstone: an in-session gather publishes to the cloud backend, and
the published version is then served by the read path — gather to read, end to
end, through the sqlite + file:// stand-in."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ietf_llm import __main__ as main_mod
from ietf_llm import gather_runner
from ietf_llm.corpus_store import get_corpus_store
from ietf_llm.utils import get_cache_dir


def _wait_done(corpus: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = gather_runner.read_status(corpus)
        if status and status.get("state") in ("done", "failed"):
            assert status["state"] == "done", status
            return
        time.sleep(0.01)
    raise AssertionError(f"gather for {corpus} did not finish in {timeout}s")

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_CONTROL_DB",
    "IETF_LLM_BLOB_DIR",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


def test_cloud_gather_publishes_and_serves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = isolated_home / "store"
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_CONTROL_DB", str(base / "control.db"))
    monkeypatch.setenv("IETF_LLM_BLOB_DIR", str(base / "bucket"))
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))

    # Stub the pipeline: write a files/ tree into the local cache (the gather's
    # natural output, which on a cloud node is ephemeral scratch), report
    # success. The worker then publishes that tree to the cloud backend.
    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None) -> bool:
        corpus = argv[0]
        digests = Path(get_cache_dir()) / corpus / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("# Overview\nbody\n")
        return True

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    _wait_done("tls")

    # A fresh store (same config) resolves the published version and
    # materialises its files for the read path.
    store = get_corpus_store()
    assert store.resolve_current("tls") is not None
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "# Overview\nbody\n"


def test_fleet_slot_blocks_a_gather_until_released(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = isolated_home / "store"
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_CONTROL_DB", str(base / "control.db"))
    monkeypatch.setenv("IETF_LLM_BLOB_DIR", str(base / "bucket"))
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))
    monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", "1")
    monkeypatch.setattr(gather_runner, "_SLOT_POLL_S", 0.02)

    def _fake_run_gather(argv: list[str], _v: object, progress: object = None) -> bool:
        digests = Path(get_cache_dir()) / argv[0] / "files" / "digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "index.md").write_text("ok\n")
        return True

    monkeypatch.setattr(main_mod, "run_gather", _fake_run_gather)

    # Another host already holds the one fleet slot.
    store = get_corpus_store()
    assert store.acquire_gather_slot("other-host", "x", 1000.0, 1) is True

    assert gather_runner.start(gather_runner.GatherSpec(corpus="tls"))["started"]
    # tls cannot get a slot, so it waits in `queued` (the worker is polling).
    deadline = time.time() + 2.0
    while time.time() < deadline:
        st = gather_runner.read_status("tls")
        if st and st.get("state") == "queued":
            break
        time.sleep(0.01)
    assert gather_runner.read_status("tls")["state"] == "queued"

    # Free the fleet slot; tls now acquires it, runs, and publishes.
    store.release_gather_slot("other-host")
    _wait_done("tls")
    assert get_corpus_store().resolve_current("tls") is not None
