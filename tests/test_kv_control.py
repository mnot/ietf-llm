"""Tests for the KvControlPlane over an in-memory KvStore: pointer, leases,
fleet slots, and status. Mirrors the old SQL control-plane behaviour."""

from __future__ import annotations

import threading
from typing import Callable, List

from ietf_llm.kv_control import KvControlPlane
from ietf_llm.kv_store import InMemoryKvStore


def _race(n: int, attempt: Callable[[int], bool]) -> int:
    """Run `attempt(i)` on `n` threads released together; return how many won."""
    barrier = threading.Barrier(n)
    wins: List[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        won = attempt(i)
        with lock:
            wins.append(won)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(wins)


def _cp() -> KvControlPlane:
    return KvControlPlane(InMemoryKvStore())


def test_set_then_resolve_current() -> None:
    cp = _cp()
    assert cp.resolve_current("tls") is None
    cp.set_current("tls", "v1")
    assert cp.resolve_current("tls") == "v1"
    cp.set_current("tls", "v2")
    assert cp.resolve_current("tls") == "v2"


def test_list_corpora_only_published_and_sorted() -> None:
    cp = _cp()
    cp.set_current("tls", "v1")
    cp.set_current("httpbis", "v1")
    assert cp.list_corpora() == ["httpbis", "tls"]


def test_lease_excludes_other_until_expiry() -> None:
    cp = _cp()
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0) is True
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1050.0) is False
    assert cp.lease_holder("tls", now=1050.0) == "node-a"
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1101.0) is True
    assert cp.lease_holder("tls", now=1101.0) == "node-b"


def test_lease_owner_reacquire_and_renew() -> None:
    cp = _cp()
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0) is True
    assert cp.acquire_lease("tls", "node-a", ttl=100.0, now=1050.0) is True
    assert cp.renew_lease("tls", "node-a", ttl=100.0, now=1090.0) is True
    assert cp.lease_holder("tls", now=1180.0) == "node-a"
    assert cp.renew_lease("tls", "node-b", ttl=100.0, now=1185.0) is False


def test_lease_release_frees_it() -> None:
    cp = _cp()
    cp.acquire_lease("tls", "node-a", ttl=100.0, now=1000.0)
    cp.release_lease("tls", "node-a")
    assert cp.lease_holder("tls", now=1001.0) is None
    assert cp.acquire_lease("tls", "node-b", ttl=100.0, now=1001.0) is True


def test_gather_slot_cap_enforced_and_idempotent() -> None:
    cp = _cp()
    assert cp.acquire_gather_slot("A", "tls", 100.0, 2, now=1000.0) is True
    assert cp.acquire_gather_slot("B", "quic", 100.0, 2, now=1000.0) is True
    assert cp.acquire_gather_slot("C", "http", 100.0, 2, now=1000.0) is False
    # Re-acquiring an already-held slot is idempotent even at the cap.
    assert cp.acquire_gather_slot("A", "tls", 100.0, 2, now=1010.0) is True


def test_gather_slot_expiry_and_release_reclaim() -> None:
    cp = _cp()
    assert cp.acquire_gather_slot("A", "tls", 10.0, 1, now=1000.0) is True
    assert cp.acquire_gather_slot("B", "quic", 10.0, 1, now=1005.0) is False
    assert cp.acquire_gather_slot("B", "quic", 10.0, 1, now=1011.0) is True
    assert cp.renew_gather_slot("B", 10.0, now=1015.0) is True
    cp.release_gather_slot("B")
    assert cp.acquire_gather_slot("A", "tls", 10.0, 1, now=1016.0) is True
    assert cp.renew_gather_slot("ZZZ", 10.0) is False


def test_concurrent_lease_acquire_has_one_winner() -> None:
    # The headline property the InMemoryKvStore lock exists to make testable:
    # many nodes racing for one corpus's lease, exactly one wins.
    cp = _cp()
    wins = _race(16, lambda i: cp.acquire_lease("tls", f"node-{i}", 100.0, now=1000.0))
    assert wins == 1
    assert cp.lease_holder("tls", now=1000.0) is not None


def test_concurrent_gather_slot_respects_cap() -> None:
    # Cap of 1, sixteen nodes racing for a fleet slot: exactly one is admitted.
    cp = _cp()
    wins = _race(16, lambda i: cp.acquire_gather_slot(f"n{i}", "tls", 100.0, 1, now=1000.0))
    assert wins == 1


def test_gather_status_roundtrip_and_listing() -> None:
    cp = _cp()
    assert cp.get_gather_status("tls") is None
    cp.set_gather_status("tls", '{"state": "running"}')
    cp.set_gather_status("quic", '{"state": "done"}')
    # A corpus with a pointer but no status is not listed.
    cp.set_current("aipref", "v1")
    assert cp.get_gather_status("tls") == '{"state": "running"}'
    assert cp.list_gather_statuses() == [
        ("quic", '{"state": "done"}'),
        ("tls", '{"state": "running"}'),
    ]


# --- read-path access marker ----------------------------------------------


def test_access_round_trips_and_is_last_writer_wins() -> None:
    cp = _cp()
    assert cp.get_access("tls") is None
    cp.set_access("tls", "2026-06-01T00:00:00Z")
    assert cp.get_access("tls") == "2026-06-01T00:00:00Z"
    # Last-writer-wins: a later stamp simply overwrites, no CAS.
    cp.set_access("tls", "2026-06-18T12:00:00Z")
    assert cp.get_access("tls") == "2026-06-18T12:00:00Z"


def test_access_is_per_corpus() -> None:
    cp = _cp()
    cp.set_access("tls", "2026-06-01T00:00:00Z")
    assert cp.get_access("httpbis") is None


# --- per-WG config keys ----------------------------------------------------


def test_config_round_trips_per_scope() -> None:
    cp = _cp()
    assert cp.get_config("tls", "gather") is None
    cp.set_config("tls", "gather", '{"mailing_list": ["tls"]}')
    cp.set_config("tls", "export", '{"dest": "nblm"}')
    assert cp.get_config("tls", "gather") == '{"mailing_list": ["tls"]}'
    assert cp.get_config("tls", "export") == '{"dest": "nblm"}'
    # Scopes are independent keys.
    cp.set_config("tls", "gather", "{}")
    assert cp.get_config("tls", "gather") == "{}"
    assert cp.get_config("tls", "export") == '{"dest": "nblm"}'


def test_clear_config_removes_all_scopes() -> None:
    cp = _cp()
    assert cp.clear_config("tls") is False  # nothing to clear
    cp.set_config("tls", "gather", "{}")
    cp.set_config("tls", "export", "{}")
    assert cp.clear_config("tls") is True
    assert cp.get_config("tls", "gather") is None
    assert cp.get_config("tls", "export") is None


def test_config_is_per_corpus() -> None:
    cp = _cp()
    cp.set_config("tls", "gather", "{}")
    assert cp.get_config("httpbis", "gather") is None
    assert cp.clear_config("httpbis") is False
