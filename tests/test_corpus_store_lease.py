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


# --- publish is fenced by the lease and the pointer (issue #288) -----------

import threading
from typing import Callable, List, Tuple

import pytest

from ietf_llm.store.corpus import LeaseLost


class _HookedBlobs(FileBlobStore):
    """A blob store that runs `hook(key)` once, just before the first put of a
    key ending in `trigger` — the moment a publisher is mid-publish."""

    def __init__(self, base: str, trigger: str = "") -> None:
        super().__init__(base)
        self.trigger = trigger
        self.hook: Callable[[str], None] = lambda key: None
        self._fired = False
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            fire = key.endswith(self.trigger) and not self._fired
            self._fired = self._fired or fire
        if fire:
            self.hook(key)
        super().put(key, data)


def _pair(tmp_path: Path, trigger: str = "") -> Tuple[CloudCorpusStore, KvControlPlane, _HookedBlobs]:
    control = KvControlPlane(InMemoryKvStore())
    blobs = _HookedBlobs(str(tmp_path / "bucket"), trigger)
    return CloudCorpusStore(control, blobs, str(tmp_path / "scratch")), control, blobs


def _ws(tmp_path: Path, name: str, body: str) -> str:
    ws = tmp_path / name
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "a.md").write_text(body)
    return str(ws)


def _served(store: CloudCorpusStore) -> str:
    root = store.local_cache_dir("tls")
    assert root is not None
    return (Path(root) / "a.md").read_text()


def test_zombie_publish_is_refused_and_pointer_stays_on_the_new_holder(tmp_path: Path) -> None:
    """The repro from #288: A's lease lapses, B takes it and publishes, and A's
    later publish must not put A's older content over B's."""
    store, control, _ = _pair(tmp_path)
    assert control.acquire_lease("tls", "A", ttl=10.0, now=0.0)
    assert control.acquire_lease("tls", "B", ttl=10.0, now=100.0)  # A lapsed
    store.publish("tls", _ws(tmp_path, "wb", "from B"), version="vB", lease_owner="B")
    assert control.renew_lease("tls", "A", ttl=10.0, now=101.0) is False
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "wa", "from A"), version="vA", lease_owner="A")
    assert store.resolve_current("tls") == "vB"
    assert _served(store) == "from B"


def test_publish_refused_when_the_lease_was_never_held(tmp_path: Path) -> None:
    store, _, blobs = _pair(tmp_path)
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "w", "x"), version="v1", lease_owner="A")
    assert store.resolve_current("tls") is None
    # Refused before the upload, not after minutes of it.
    assert not blobs.list_prefix("corpora/tls/versions/v1/")


def test_lease_taken_over_during_the_upload_refuses_the_flip(tmp_path: Path) -> None:
    """A passes the start-of-publish check, then stalls while uploading; its
    lease lapses, B takes it and publishes a complete version. A resumes: the
    confirm-and-flip must refuse, and B's version stays current."""
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    control.set_current("tls", "v0")

    def b_takes_over_and_publishes(key: str) -> None:
        # Modelled as expiry: rewrite A's lease as lapsed, then B acquires.
        control.release_lease("tls", "A")
        assert control.acquire_lease("tls", "B", ttl=3600.0)
        other = CloudCorpusStore(control, FileBlobStore(str(tmp_path / "bucket")), str(tmp_path / "s2"))
        other.publish("tls", _ws(tmp_path, "wb", "from B"), version="vB", lease_owner="B")

    blobs.hook = b_takes_over_and_publishes
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "wa", "from A"), version="vA", lease_owner="A")
    assert store.resolve_current("tls") == "vB"
    assert _served(store) == "from B"


def test_lease_taken_over_but_not_yet_published_refuses_the_flip(tmp_path: Path) -> None:
    """B has taken the lease but not published yet when A reaches the flip. The
    pointer has not moved, so only the lease re-proof can refuse A -- and it must,
    or A lands its older content in the middle of B's gather."""
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    control.set_current("tls", "v0")

    def b_takes_over(key: str) -> None:
        control.release_lease("tls", "A")  # A lapses ...
        assert control.acquire_lease("tls", "B", ttl=3600.0)  # ... B takes it

    blobs.hook = b_takes_over
    with pytest.raises(LeaseLost, match="taken over during the upload"):
        store.publish("tls", _ws(tmp_path, "wa", "from A"), version="vA", lease_owner="A")
    assert store.resolve_current("tls") == "v0"


def test_pointer_moved_during_the_upload_refuses_the_flip(tmp_path: Path) -> None:
    """Even holding the lease, a publisher whose pointer token went stale (here
    an unleased writer flipped it) does not overwrite what it never saw."""
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    control.set_current("tls", "v0")
    blobs.hook = lambda key: control.set_current("tls", "vX")
    with pytest.raises(LeaseLost, match="changed during publish"):
        store.publish("tls", _ws(tmp_path, "wa", "from A"), version="vA", lease_owner="A")
    assert store.resolve_current("tls") == "vX"


def test_first_publishers_racing_on_an_empty_pointer_have_one_winner(tmp_path: Path) -> None:
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    blobs.hook = lambda key: control.set_current("tls", "vX")  # the other first publisher
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "wa", "A"), version="vA")
    assert store.resolve_current("tls") == "vX"


def test_lapsed_but_unclaimed_lease_still_publishes(tmp_path: Path) -> None:
    """Expiry alone does not block the holder: with no taker the record still
    names it, and the pointer compare-and-swap is what orders it against anyone
    who takes the lease later."""
    store, control, _ = _pair(tmp_path)
    control.acquire_lease("tls", "A", ttl=1.0, now=0.0)  # long expired by now
    assert store.publish("tls", _ws(tmp_path, "w", "ok"), version="v1", lease_owner="A") == "v1"
    assert _served(store) == "ok"


def test_holder_publishes_normally_with_a_live_lease_and_heartbeat(tmp_path: Path) -> None:
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    # The heartbeat renews mid-publish; the confirm must not mistake it for a loss.
    blobs.hook = lambda key: control.renew_lease("tls", "A", ttl=3600.0, now=10.0)
    assert store.publish("tls", _ws(tmp_path, "w", "ok"), version="v1", lease_owner="A") == "v1"
    assert store.resolve_current("tls") == "v1"


def test_zombie_and_holder_publishing_concurrently_never_let_the_zombie_win(tmp_path: Path) -> None:
    """A stress version of the repro: whatever the interleaving of the two
    publishes, the zombie is refused and the holder's version is current."""
    for round_no in range(25):
        store, control, _ = _pair(tmp_path / f"r{round_no}")
        control.acquire_lease("tls", "A", ttl=10.0, now=0.0)
        control.acquire_lease("tls", "B", ttl=10.0, now=100.0)
        wa = _ws(tmp_path / f"r{round_no}", "wa", "A")
        wb = _ws(tmp_path / f"r{round_no}", "wb", "B")
        barrier = threading.Barrier(2)
        errors: List[BaseException] = []

        def publish(ws: str, version: str, owner: str) -> None:
            barrier.wait()
            try:
                store.publish("tls", ws, version=version, lease_owner=owner)
            except LeaseLost as err:
                errors.append(err)

        threads = [
            threading.Thread(target=publish, args=(wa, "vA", "A")),
            threading.Thread(target=publish, args=(wb, "vB", "B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 1, round_no
        assert store.resolve_current("tls") == "vB"


# --- a refused publish removes its own staged version ----------------------


def _versions(store: CloudCorpusStore) -> List[str]:
    return store._list_versions("tls")  # pylint: disable=protected-access


def _published_by_a(tmp_path: Path) -> Tuple[CloudCorpusStore, KvControlPlane, _HookedBlobs]:
    """A holds the lease and has published v1 and v2, so v2 is current and v1 is
    the "previous" version a stale replica may still be reading."""
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    store.publish("tls", _ws(tmp_path, "w1", "one"), version="v1", lease_owner="A")
    blobs._fired = False  # re-arm the hook for the publish under test
    store.publish("tls", _ws(tmp_path, "w2", "two"), version="v2", lease_owner="A")
    blobs._fired = False
    return store, control, blobs


def test_refused_publish_leaves_no_orphan_to_displace_the_previous_version(tmp_path: Path) -> None:
    """A lapses mid-publish and B takes over; A's refused publish carries the
    newest id. Left behind, it would rank as "previous" and B's next publish
    would reap the real previous version (v2) instead of it."""
    store, control, blobs = _published_by_a(tmp_path)

    def b_takes_over(key: str) -> None:
        control.release_lease("tls", "A")
        assert control.acquire_lease("tls", "B", ttl=3600.0)

    blobs.hook = b_takes_over
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "w3", "from A"), version="v3", lease_owner="A")
    assert _versions(store) == ["v1", "v2"]  # A's staged v3 is gone
    store.publish("tls", _ws(tmp_path, "w4", "from B"), version="v4", lease_owner="B")
    # current + previous survive the reap; with the orphan kept, v3 would have
    # ranked as previous and v2 been reaped.
    assert _versions(store) == ["v2", "v4"]
    assert _served(store) == "from B"


def test_publish_refused_by_the_pointer_also_removes_its_staged_version(tmp_path: Path) -> None:
    store, control, blobs = _published_by_a(tmp_path)
    blobs.hook = lambda key: control.set_current("tls", "vX")
    with pytest.raises(LeaseLost, match="changed during publish"):
        store.publish("tls", _ws(tmp_path, "w3", "from A"), version="v3", lease_owner="A")
    assert "v3" not in _versions(store)


def test_a_failing_cleanup_does_not_mask_the_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, control, blobs = _pair(tmp_path, trigger="manifest.json")
    control.acquire_lease("tls", "A", ttl=3600.0, now=0.0)
    control.set_current("tls", "v0")
    blobs.hook = lambda key: control.set_current("tls", "vX")

    def boom(prefix: str) -> None:
        raise OSError("bucket unreachable")

    monkeypatch.setattr(blobs, "delete_prefix", boom)
    with pytest.raises(LeaseLost):
        store.publish("tls", _ws(tmp_path, "w", "x"), version="vA", lease_owner="A")
    assert store.resolve_current("tls") == "vX"


def test_a_successful_publish_keeps_its_version(tmp_path: Path) -> None:
    store, _, _ = _published_by_a(tmp_path)
    assert _versions(store) == ["v1", "v2"]
    assert _served(store) == "two"
