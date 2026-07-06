"""The gather lease on the CorpusStore port: a real cross-host lease on the
cloud backend, a no-op on the local backend."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.corpus import LocalCorpusStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore


def _cloud(tmp_path: Path) -> CloudCorpusStore:
    control = KvControlPlane(InMemoryKvStore())
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


def test_local_gather_slot_is_noop_always_granted() -> None:
    store = LocalCorpusStore()
    # No fleet to bound on a single box: every slot request is granted.
    assert store.acquire_gather_slot("A", "tls", 100.0, 1) is True
    assert store.acquire_gather_slot("B", "quic", 100.0, 1) is True
    assert store.renew_gather_slot("B", 100.0) is True
    store.release_gather_slot("B")


def test_cloud_gather_slot_enforces_fleet_cap(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    # Cap 1: one slot fleet-wide. The second owner is refused until release.
    assert store.acquire_gather_slot("A", "tls", 1000.0, 1) is True
    assert store.acquire_gather_slot("B", "quic", 1000.0, 1) is False
    store.release_gather_slot("A")
    assert store.acquire_gather_slot("B", "quic", 1000.0, 1) is True
