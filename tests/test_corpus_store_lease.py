"""The gather lease on the CorpusStore port: a real cross-host lease on the
cloud backend, a no-op on the local backend."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.corpus_control import SqliteControlPlane
from ietf_llm.corpus_store import LocalCorpusStore
from ietf_llm.corpus_store_cloud import CloudCorpusStore


def _cloud(tmp_path: Path) -> CloudCorpusStore:
    control = SqliteControlPlane(str(tmp_path / "control.db"))
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    return CloudCorpusStore(control, blobs, str(tmp_path / "scratch"))


def test_local_lease_is_noop_always_granted() -> None:
    store = LocalCorpusStore()
    # The local backend needs no distributed lease; every call is granted,
    # even to a different owner, so the existing file lock stays in charge.
    assert store.acquire_lease("tls", "node-a", 100.0) is True
    assert store.acquire_lease("tls", "node-b", 100.0) is True
    assert store.renew_lease("tls", "node-b", 100.0) is True
    store.release_lease("tls", "node-b")


def test_cloud_lease_excludes_other_holder(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    # ttl is long relative to the test's wall-clock, so node-a's lease stays
    # live throughout — node-b is excluded until node-a releases.
    assert store.acquire_lease("tls", "node-a", 1000.0) is True
    assert store.acquire_lease("tls", "node-b", 1000.0) is False
    assert store.renew_lease("tls", "node-a", 1000.0) is True
    store.release_lease("tls", "node-a")
    assert store.acquire_lease("tls", "node-b", 1000.0) is True
