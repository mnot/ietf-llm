"""Feature 6 capstone: an in-session gather publishes to the cloud backend, and
the published version is then served by the read path — gather to read, end to
end, over an S3-compatible object store (moto in-process)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import boto3
import pytest
from moto import mock_aws

from ietf_llm import __main__ as main_mod
from ietf_llm import corpus, gather_runner
from ietf_llm.corpus_store import get_corpus_store
from ietf_llm.corpus_store_cloud import _clear_resolve_cache
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
    "IETF_LLM_STORE_URL",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cloud_s3(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Select the cloud backend over an in-process moto S3 bucket that holds
    both the version content and the control-plane keys."""
    base = isolated_home / "store"
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_STORE_URL", "s3://test-bucket")
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("IETF_LLM_STORE_ENDPOINT_URL", raising=False)
    _clear_resolve_cache()  # the resolve cache is keyed by store URL; isolate
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket="test-bucket")
        yield
    _clear_resolve_cache()


def test_cloud_gather_publishes_and_serves(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the pipeline: write a files/ tree into the local cache (the gather's
    # natural output, which on a cloud node is ephemeral scratch), report
    # success. The worker then publishes that tree to the cloud backend.
    def _fake_run_gather(argv: list[str], _verbosity: object, progress: object = None, note_fn: object = None) -> bool:
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


def test_listing_classification_degrades_then_reads_through_seam(
    cloud_s3: None, isolated_home: Path
) -> None:
    """`kind_status` / `describe` (the `list_corpora` classification) must go
    through the CorpusStore seam: never create a junk local cache dir and never
    download a corpus's blobs just to classify it. A corpus not yet staged on
    this replica degrades to the config-only path; once a real read stages it,
    classification reads `group.md` from the staged tree."""
    base = isolated_home / "store"
    # Publish a group corpus straight to the cloud store: a files/group.md
    # carrying a name and status.
    workspace = isolated_home / "ws"
    (workspace / "files").mkdir(parents=True)
    (workspace / "files" / "group.md").write_text(
        "# tls\n**Name:** Transport Layer Security\n**Status:** active\n"
    )
    store = get_corpus_store()
    store.publish("tls", str(workspace))

    # Before any read materialises tls, classification degrades to config-only
    # (no config -> custom / empty) WITHOUT downloading blobs or creating a dir.
    assert corpus.kind_status("tls") == ("custom", "")
    assert corpus.describe("tls") == ""
    assert not (Path(get_cache_dir()) / "tls" / "files").exists()
    assert not (base / "scratch" / "tls").exists()

    # A real read stages the version onto scratch (the seam materialises here).
    assert get_corpus_store().local_cache_dir("tls") is not None

    # Now the same classification reads group.md through the staged tree.
    assert corpus.kind_status("tls") == ("group", "active")
    assert corpus.describe("tls") == "Transport Layer Security"
    # Still no junk dir under the local cache root — the seam owns scratch.
    assert not (Path(get_cache_dir()) / "tls" / "files").exists()


def test_all_statuses_includes_control_plane_on_cloud(cloud_s3: None) -> None:
    # On the cloud backend, a no-corpus gather_status listing must see gathers
    # recorded in the control plane by other replicas, not only the corpora
    # cached on this host (which here is none).
    store = get_corpus_store()
    store.put_gather_status(
        "tls", {"corpus": "tls", "state": "running", "updated": "2026-06-06T00:00:00Z"}
    )
    store.put_gather_status(
        "quic", {"corpus": "quic", "state": "queued", "updated": "2026-06-06T00:01:00Z"}
    )
    corpora = {s.get("corpus") for s in gather_runner.all_statuses()}
    assert {"tls", "quic"} <= corpora


def test_fleet_slot_blocks_a_gather_until_released(
    cloud_s3: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", "1")
    monkeypatch.setattr(gather_runner, "_SLOT_POLL_S", 0.02)

    def _fake_run_gather(argv: list[str], _v: object, progress: object = None, note_fn: object = None) -> bool:
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
