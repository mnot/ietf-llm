"""Tests for the KvControlPlane over an in-memory KvStore: pointer, leases,
fleet slots, and status. Mirrors the old SQL control-plane behaviour."""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, List, Optional

from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import ANY, InMemoryKvStore


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


# --- conditional pointer flip ---------------------------------------------


def test_set_current_expect_absent_then_token() -> None:
    cp = _cp()
    first = cp.pointer_token("tls")
    assert cp.set_current("tls", "v1", expect=first) is True
    # The token read before the flip is now stale: a second publisher that read
    # it too loses.
    assert cp.set_current("tls", "v2", expect=first) is False
    assert cp.resolve_current("tls") == "v1"
    assert cp.set_current("tls", "v2", expect=cp.pointer_token("tls")) is True
    assert cp.resolve_current("tls") == "v2"


def test_set_current_race_has_one_winner() -> None:
    cp = _cp()
    token = cp.pointer_token("tls")
    wins = _race(8, lambda i: cp.set_current("tls", f"v{i}", expect=token))
    assert wins == 1


# --- lease proof and renewal ----------------------------------------------


class _InterposedKv(InMemoryKvStore):
    """An in-memory store that runs `before_put(key)` just before each put, so a
    test can slip a competing write between a get and its compare-and-swap."""

    def __init__(self) -> None:
        super().__init__()
        self.before_put: Callable[[str], None] = lambda key: None

    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        self.before_put(key)
        return super().put(key, value, expect=expect)


def test_confirm_lease_and_the_expired_holder() -> None:
    cp = _cp()
    assert cp.lease_holder("tls", expired_ok=True) is None
    assert cp.confirm_lease("tls", "a") is False
    cp.acquire_lease("tls", "a", ttl=100.0, now=1000.0)
    assert cp.confirm_lease("tls", "a") is True
    assert cp.confirm_lease("tls", "b") is False
    # A lapsed lease still names its owner until someone takes it.
    later = time.time() + 5000.0
    assert cp.lease_holder("tls", now=later) is None
    assert cp.lease_holder("tls", now=later, expired_ok=True) == "a"


def test_confirm_lease_extends_the_lease_by_its_own_span() -> None:
    # Half the lease is gone when the publisher confirms; the flip and the reap
    # after it must still run under a live lease, so a taker is held off for a
    # fresh span from the confirm, not from the original grant.
    cp = _cp()
    t0 = time.time()
    cp.acquire_lease("tls", "a", ttl=100.0, now=t0 - 50)
    assert cp.acquire_lease("tls", "b", ttl=100.0, now=t0 + 60) is True  # lapsed at t0+50
    cp = _cp()
    cp.acquire_lease("tls", "a", ttl=100.0, now=t0 - 50)
    assert cp.confirm_lease("tls", "a") is True
    assert cp.acquire_lease("tls", "b", ttl=100.0, now=t0 + 60) is False
    assert cp.acquire_lease("tls", "b", ttl=100.0, now=t0 + 101) is True


def test_confirm_lease_never_shortens_and_changes_the_record() -> None:
    kv = InMemoryKvStore()
    cp = KvControlPlane(kv)
    cp.acquire_lease("tls", "a", ttl=100.0, now=time.time() + 10_000)  # far future
    before = kv.get("corpora/tls/lease")
    assert cp.confirm_lease("tls", "a") is True
    after = kv.get("corpora/tls/lease")
    assert before is not None and after is not None
    # Not shortened below the grant; (the token changes: see the S3 test for the
    # byte-level proof that a real ETag does too).
    assert json.loads(after[0])["expires_at"] >= json.loads(before[0])["expires_at"]


def test_confirm_lease_survives_a_concurrent_heartbeat_renewal() -> None:
    # The heartbeat renews the same record while a publish confirms it. A CAS
    # lost to our own heartbeat is not a lost lease.
    kv = _InterposedKv()
    cp = KvControlPlane(kv)
    cp.acquire_lease("tls", "a", ttl=100.0, now=1000.0)
    fired: List[str] = []

    def heartbeat_lands_first(key: str) -> None:
        if key.endswith("/lease") and not fired:
            fired.append(key)
            cp.renew_lease("tls", "a", ttl=100.0, now=1500.0)

    kv.before_put = heartbeat_lands_first
    assert cp.confirm_lease("tls", "a") is True
    assert fired


def test_renew_lease_survives_a_concurrent_renewal() -> None:
    kv = _InterposedKv()
    cp = KvControlPlane(kv)
    cp.acquire_lease("tls", "a", ttl=100.0, now=1000.0)
    fired: List[str] = []

    def other_thread_renews(key: str) -> None:
        if key.endswith("/lease") and not fired:
            fired.append(key)
            cp.confirm_lease("tls", "a")

    kv.before_put = other_thread_renews
    assert cp.renew_lease("tls", "a", ttl=100.0, now=1200.0) is True
    assert fired
    # And the renewal took: the lease runs to 1300, not the original 1100.
    assert cp.acquire_lease("tls", "b", ttl=100.0, now=1250.0) is False


def test_renew_and_confirm_refused_once_taken_over_mid_call() -> None:
    # A takeover that lands between our get and our CAS must read as a lost
    # lease, not be retried into success.
    for op in ("renew", "confirm"):
        kv = _InterposedKv()
        cp = KvControlPlane(kv)
        cp.acquire_lease("tls", "a", ttl=100.0, now=1000.0)
        fired: List[str] = []

        def b_takes_over(key: str) -> None:
            if key.endswith("/lease") and not fired:
                fired.append(key)
                assert cp.acquire_lease("tls", "b", ttl=100.0, now=2000.0)

        kv.before_put = b_takes_over
        if op == "renew":
            assert cp.renew_lease("tls", "a", ttl=100.0, now=2000.0) is False
        else:
            assert cp.confirm_lease("tls", "a") is False
        assert cp.lease_holder("tls", now=2000.0) == "b"


def test_renew_refused_for_a_takeover_even_after_release() -> None:
    cp = _cp()
    cp.acquire_lease("tls", "a", ttl=10.0, now=0.0)
    cp.acquire_lease("tls", "b", ttl=10.0, now=100.0)
    cp.release_lease("tls", "b")
    # B released, but the record still names B: A's zombie renewal is refused.
    assert cp.renew_lease("tls", "a", ttl=10.0, now=200.0) is False
    assert cp.lease_holder("tls", expired_ok=True) == "b"


def test_gather_slot_retries_a_lost_cas() -> None:
    # S3's 409 ConditionalRequestConflict surfaces from the KvStore as a lost
    # CAS (None); the slot loops must retry it rather than fail the gather.
    kv = _InterposedKv()
    cp = KvControlPlane(kv)
    lost: List[str] = []
    real_put = InMemoryKvStore.put

    def put_losing_once(key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        if key == "fleet/slots" and not lost:
            lost.append(key)
            return None
        return real_put(kv, key, value, expect=expect)

    kv.put = put_losing_once  # type: ignore[method-assign]
    assert cp.acquire_gather_slot("A", "tls", ttl=100.0, max_inflight=1, now=0.0) is True
    assert lost
