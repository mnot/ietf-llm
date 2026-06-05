"""Tests for the SQLite control plane: versions, current pointer, leases."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.corpus_control import SqliteControlPlane


def _cp(tmp_path: Path) -> SqliteControlPlane:
    return SqliteControlPlane(str(tmp_path / "control.db"))


def test_publish_then_resolve_and_manifest(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.publish_version("tls", "v1", {"objects": {"a": "k1"}})
    assert cp.resolve_current("tls") == "v1"
    assert cp.get_manifest("tls", "v1") == {"objects": {"a": "k1"}}


def test_pointer_moves_to_latest_and_history_kept(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.publish_version("tls", "v1", {"n": 1})
    cp.publish_version("tls", "v2", {"n": 2})
    assert cp.resolve_current("tls") == "v2"
    # Old versions stay retrievable — versions are immutable history.
    assert cp.get_manifest("tls", "v1") == {"n": 1}
    assert cp.get_manifest("tls", "v2") == {"n": 2}


def test_unknown_corpus(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.resolve_current("ghost") is None
    assert cp.get_manifest("ghost", "v1") is None
    assert cp.list_corpora() == []


def test_list_corpora_sorted(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.publish_version("tls", "v1", {})
    cp.publish_version("httpbis", "v1", {})
    assert cp.list_corpora() == ["httpbis", "tls"]


def test_lease_excludes_other_until_expiry(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0) is True
    # A different node cannot take it while node-a's lease is live.
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1050.0) is False
    assert cp.lease_holder("tls", now=1050.0) == "node-a"
    # Once node-a's lease has expired, node-b can take it.
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1101.0) is True
    assert cp.lease_holder("tls", now=1101.0) == "node-b"


def test_lease_owner_reacquire_and_renew(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0) is True
    # Same owner re-acquiring is fine (a heartbeat that also resets the clock).
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1050.0) is True
    assert cp.renew_lease("tls", "node-a", ttl=100.0, now=1090.0) is True
    assert cp.lease_holder("tls", now=1180.0) == "node-a"
    # A non-holder cannot renew.
    assert cp.renew_lease("tls", "node-b", ttl=100.0, now=1185.0) is False


def test_lease_release_frees_it(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0)
    cp.release_lease("tls", "node-a")
    assert cp.lease_holder("tls", now=1001.0) is None
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1001.0) is True


def test_gather_slot_cap_enforced_and_idempotent(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    # Cap of 2: two distinct owners get a slot, a third is refused.
    assert cp.acquire_gather_slot("A", "tls", 100.0, 2, now=1000.0) is True
    assert cp.acquire_gather_slot("B", "quic", 100.0, 2, now=1000.0) is True
    assert cp.acquire_gather_slot("C", "http", 100.0, 2, now=1000.0) is False
    # Re-acquiring an already-held slot is idempotent even at the cap.
    assert cp.acquire_gather_slot("A", "tls", 100.0, 2, now=1010.0) is True


def test_gather_slot_expiry_and_release_reclaim(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    assert cp.acquire_gather_slot("A", "tls", 10.0, 1, now=1000.0) is True
    # Cap 1: B is blocked while A's slot is live.
    assert cp.acquire_gather_slot("B", "quic", 10.0, 1, now=1005.0) is False
    # Once A's slot expires, B can take it.
    assert cp.acquire_gather_slot("B", "quic", 10.0, 1, now=1011.0) is True
    # Renew keeps B's; release frees it for the next caller.
    assert cp.renew_gather_slot("B", 10.0, now=1015.0) is True
    cp.release_gather_slot("B")
    assert cp.acquire_gather_slot("A", "tls", 10.0, 1, now=1016.0) is True
    assert cp.renew_gather_slot("ZZZ", 10.0) is False  # no slot held
