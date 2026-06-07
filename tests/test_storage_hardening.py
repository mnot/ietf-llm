"""Tests for the concurrency / correctness hardening from the PR #68 review
(findings G-1..G-10)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from ietf_llm import corpus_control
from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.corpus_control import SqliteControlPlane
from ietf_llm.corpus_store import LocalCorpusStore, get_corpus_store
from ietf_llm.corpus_store_cloud import CloudCorpusStore, _clear_resolve_cache
from ietf_llm.gather_runner import _owner
from ietf_llm.kv_control import KvControlPlane
from ietf_llm.kv_store import InMemoryKvStore

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_STORE_URL",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


# G-9: an unrecognised backend raises rather than silently using Local.
def test_unrecognised_backend_raises(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "Cloud")  # wrong case
    with pytest.raises(ValueError):
        get_corpus_store()


def test_default_backend_is_still_local(isolated_home: Path) -> None:
    assert isinstance(get_corpus_store(), LocalCorpusStore)


# G-10: the lease owner id carries a per-process nonce (host:pid:nonce).
def test_owner_has_per_process_nonce() -> None:
    parts = _owner().split(":")
    assert len(parts) == 3 and all(parts)


# G-3: the SQLite schema is ensured at most once per process per db path.
def test_sqlite_schema_ensured_once(tmp_path: Path) -> None:
    path = str(tmp_path / "c.db")
    corpus_control._sqlite_schema_ensured.discard(path)
    SqliteControlPlane(path)
    assert path in corpus_control._sqlite_schema_ensured
    # A second construction is a no-op for ensure_schema (still works).
    SqliteControlPlane(path).resolve_current("nope")


def _cloud(tmp_path: Path) -> CloudCorpusStore:
    return CloudCorpusStore(
        KvControlPlane(InMemoryKvStore()),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
    )


def _publish_tls(store: CloudCorpusStore, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text("hi")
    (ws / "last-gathered").write_text("x")
    store.publish("tls", str(ws), version="v1")


# G-5/G-6: a complete materialise serves the whole tree (atomic temp+rename).
def test_materialise_serves_complete_tree(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    _publish_tls(store, tmp_path)
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "hi"
    # No leftover temp dirs in scratch.
    assert not list((tmp_path / "scratch" / "tls").glob("*.tmp.*"))


# G-7: a lost blob fails loudly (manifest-verified) instead of silently omitting.
def test_materialise_fails_on_missing_blob(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    _publish_tls(store, tmp_path)
    # Simulate a lost/durability-gap blob: delete one object from the bucket.
    blob = (
        tmp_path
        / "bucket"
        / "corpora"
        / "tls"
        / "versions"
        / "v1"
        / "files"
        / "digests"
        / "index.md"
    )
    blob.unlink()
    with pytest.raises(FileNotFoundError):
        store.local_cache_dir("tls")


def _publish_with_index(store: CloudCorpusStore, tmp_path: Path) -> None:
    ws = tmp_path / "wsidx"
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "x.md").write_text("f")
    (ws / "embeddings.db").write_bytes(b"DB")
    store.publish("tls", str(ws), version="v1")


# G-2: the search index resolves through the store.
def test_local_index_dir_is_index_root(isolated_home: Path) -> None:
    from ietf_llm.utils import get_index_dir

    assert LocalCorpusStore().local_index_dir("tls") == os.path.join(
        get_index_dir(), "tls"
    )


def test_cloud_index_dir_materialises_db(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    _publish_with_index(store, tmp_path)
    idx = store.local_index_dir("tls")
    assert idx is not None
    assert (Path(idx) / "embeddings.db").read_bytes() == b"DB"


def test_db_path_ro_routes_through_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm import corpus_store as cs_mod
    from ietf_llm.embeddings import storage as storage_mod

    store = _cloud(tmp_path)
    _publish_with_index(store, tmp_path)
    monkeypatch.setattr(cs_mod, "get_corpus_store", lambda: store)
    path = storage_mod._db_path_ro("tls")
    assert path.endswith("embeddings.db")
    assert os.path.isfile(path)  # the version's index was materialised
    with open(path, "rb") as handle:
        assert handle.read() == b"DB"


# G-1: a request-scoped pin keeps all reads on one version across a mid-request
# publish (files and index both stay pinned).
def test_version_pin_holds_across_publish(tmp_path: Path) -> None:
    from ietf_llm.corpus_store import pin_corpus_version

    store = _cloud(tmp_path)
    _publish_with_index(store, tmp_path)  # v1: files/x.md == "f"
    v1 = store.resolve_current("tls")
    assert v1 is not None
    with pin_corpus_version("tls", v1):
        ws2 = tmp_path / "ws2"
        (ws2 / "files").mkdir(parents=True)
        (ws2 / "files" / "x.md").write_text("v2content")
        (ws2 / "embeddings.db").write_bytes(b"DB2")
        store.publish("tls", str(ws2), version="v2")
        assert store.resolve_current("tls") == "v2"  # the pointer moved...
        # ...but pinned reads still serve v1, for both files and the index.
        cache = store.local_cache_dir("tls")
        assert cache is not None and (Path(cache) / "x.md").read_text() == "f"
        idx = store.local_index_dir("tls")
        assert idx is not None and idx.endswith(v1)
    # Outside the pin, reads follow the current version (v2).
    idx2 = store.local_index_dir("tls")
    assert idx2 is not None and idx2.endswith("v2")


# G-4: the lease heartbeat renews until stopped, and bails if the lease is lost.
class _RenewSpy:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: List[Tuple[str, str]] = []

    def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        self.calls.append((corpus, owner))
        return self.result


def test_heartbeat_renews_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import gather_runner as gr

    monkeypatch.setattr(gr, "_LEASE_HEARTBEAT_S", 0.01)
    spy: Any = _RenewSpy(result=True)
    stop = threading.Event()
    t = threading.Thread(target=gr._heartbeat_lease, args=(spy, "tls", "me", stop))
    t.start()
    time.sleep(0.06)
    stop.set()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert len(spy.calls) >= 1 and spy.calls[0] == ("tls", "me")


def test_heartbeat_stops_when_lease_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import gather_runner as gr

    monkeypatch.setattr(gr, "_LEASE_HEARTBEAT_S", 0.01)
    spy: Any = _RenewSpy(result=False)  # lease was stolen after an expiry
    stop = threading.Event()
    t = threading.Thread(target=gr._heartbeat_lease, args=(spy, "tls", "me", stop))
    t.start()
    t.join(timeout=1.0)
    assert not t.is_alive()  # exited on its own once renew returned False


# G-8: gather status is fleet-visible via the control plane on the cloud backend.
def test_cloud_gather_status_roundtrip(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    store.put_gather_status("tls", {"state": "running", "stage": "drafts"})
    got = store.get_gather_status("tls")
    assert got is not None
    # No live lease -> a `running` record is relabelled `interrupted` (crashed).
    assert got["state"] == "interrupted"
    assert got["stage"] == "drafts"


def test_cloud_gather_status_running_with_live_lease(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    store.acquire_lease("tls", "node-a", 1000.0)
    store.put_gather_status("tls", {"state": "running"})
    got = store.get_gather_status("tls")
    assert got is not None and got["state"] == "running"  # the lease is live


def test_local_gather_status_is_noop(isolated_home: Path) -> None:
    store = LocalCorpusStore()
    store.put_gather_status("tls", {"state": "running"})  # no-op
    assert store.get_gather_status("tls") is None  # local backend uses the file


# --- resolve-version cache (short-TTL coalescing of current-version lookups) ---


def _counting_cloud(
    tmp_path: Path, name: str = "cloud", ttl: float = 60.0
) -> Tuple[CloudCorpusStore, Dict[str, int]]:
    """A cloud store whose control-plane `resolve_current` is wrapped with a call
    counter, with caching keyed by `name` (so stores don't share entries)."""
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    control = KvControlPlane(InMemoryKvStore())
    counter: Dict[str, int] = {"n": 0}
    real = control.resolve_current

    def counting(corpus: str) -> Any:
        counter["n"] += 1
        return real(corpus)

    control.resolve_current = counting  # type: ignore[method-assign]
    store = CloudCorpusStore(
        control,
        FileBlobStore(str(base / "bucket")),
        str(base / "scratch"),
        resolve_ttl=ttl,
        cache_key=name,
    )
    return store, counter


def _publish_version(store: CloudCorpusStore, tmp_path: Path, name: str) -> None:
    ws = tmp_path / f"ws-{name}"
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "x.md").write_text("f")
    store.publish("tls", str(ws), version="v1")


def test_resolve_cache_coalesces_reads(tmp_path: Path) -> None:
    _clear_resolve_cache()
    store, counter = _counting_cloud(tmp_path)
    _publish_version(store, tmp_path, "cloud")
    _clear_resolve_cache()  # cold cache: ignore the publish write-through
    counter["n"] = 0
    for _ in range(5):
        assert store.resolve_current("tls") == "v1"
    assert counter["n"] == 1  # five reads, one control-plane call


def test_resolve_cache_write_through_on_publish(tmp_path: Path) -> None:
    _clear_resolve_cache()
    store, counter = _counting_cloud(tmp_path)
    _publish_version(store, tmp_path, "cloud")
    counter["n"] = 0
    # The publisher serves what it just published with no extra round trip.
    assert store.resolve_current("tls") == "v1"
    assert counter["n"] == 0


def test_resolve_cache_disabled_when_ttl_zero(tmp_path: Path) -> None:
    _clear_resolve_cache()
    store, counter = _counting_cloud(tmp_path, ttl=0.0)
    _publish_version(store, tmp_path, "cloud")
    counter["n"] = 0
    for _ in range(3):
        store.resolve_current("tls")
    assert counter["n"] == 3  # no caching: every read hits the control plane


def test_resolve_cache_negative_caching(tmp_path: Path) -> None:
    _clear_resolve_cache()
    store, counter = _counting_cloud(tmp_path)
    assert store.resolve_current("ghost") is None
    assert store.resolve_current("ghost") is None
    assert counter["n"] == 1  # the absent result is cached too


def test_resolve_cache_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ietf_llm import corpus_store_cloud as csc

    clock = {"t": 1000.0}
    monkeypatch.setattr(csc.time, "monotonic", lambda: clock["t"])
    _clear_resolve_cache()
    store, counter = _counting_cloud(tmp_path, ttl=10.0)
    _publish_version(store, tmp_path, "cloud")
    _clear_resolve_cache()
    counter["n"] = 0
    store.resolve_current("tls")  # caches until t=1010
    clock["t"] = 1005.0
    store.resolve_current("tls")  # still fresh
    assert counter["n"] == 1
    clock["t"] = 1011.0
    store.resolve_current("tls")  # expired -> re-resolves
    assert counter["n"] == 2


def test_resolve_cache_scoped_per_control_plane(tmp_path: Path) -> None:
    _clear_resolve_cache()
    store_a, count_a = _counting_cloud(tmp_path, name="A")
    store_b, count_b = _counting_cloud(tmp_path, name="B")
    _publish_version(store_a, tmp_path, "A")  # only A has a version
    _clear_resolve_cache()
    count_a["n"] = count_b["n"] = 0
    assert store_a.resolve_current("tls") == "v1"
    assert store_b.resolve_current("tls") is None  # B's pointer is empty...
    # ...and B's miss didn't read A's cached entry (separate cache_key).
    assert count_a["n"] == 1 and count_b["n"] == 1


# --- G-2 residual: a split-out index dir is still captured into the version ---


def test_publish_includes_extra_files(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    ws = tmp_path / "ws"
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "x.md").write_text("f")
    idx = tmp_path / "fastindex" / "tls" / "embeddings.db"
    idx.parent.mkdir(parents=True)
    idx.write_bytes(b"SPLITDB")
    store.publish(
        "tls", str(ws), version="v1", extra_files={"embeddings.db": str(idx)}
    )
    got = store.local_index_dir("tls")  # materialise the version
    assert got is not None
    assert (Path(got) / "embeddings.db").read_bytes() == b"SPLITDB"
    assert (Path(got) / "files" / "x.md").read_text() == "f"


def test_index_extra_files_empty_when_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm import gather_runner as gr

    ws = tmp_path / "cache" / "tls"
    ws.mkdir(parents=True)
    (ws / "embeddings.db").write_bytes(b"DB")
    # Default layout: index dir == cache, so the version walk already gets it.
    monkeypatch.setattr(gr, "get_index_dir", lambda: str(tmp_path / "cache"))
    assert gr._index_extra_files("tls", str(ws)) == {}


def test_index_extra_files_captures_split_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm import gather_runner as gr

    ws = tmp_path / "cache" / "tls"
    ws.mkdir(parents=True)
    split = tmp_path / "fastindex"
    (split / "tls").mkdir(parents=True)
    (split / "tls" / "embeddings.db").write_bytes(b"DB")
    (split / "tls" / "embeddings.db-wal").write_bytes(b"W")  # sidecar too
    monkeypatch.setattr(gr, "get_index_dir", lambda: str(split))
    extras = gr._index_extra_files("tls", str(ws))
    assert set(extras) == {"embeddings.db", "embeddings.db-wal"}
    assert extras["embeddings.db"] == str(split / "tls" / "embeddings.db")


# --- scratch reaper: bound per-replica materialised versions ---


def test_reaper_removes_superseded_version(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    for ver, content in (("v1", "a"), ("v2", "b")):
        ws = tmp_path / f"ws-{ver}"
        (ws / "files").mkdir(parents=True)
        (ws / "files" / "x.md").write_text(content)
        store.publish("tls", str(ws), version=ver)
        store.local_cache_dir("tls")  # materialise the (new) current version
    scratch = tmp_path / "scratch" / "tls"
    versions = sorted(
        p.name for p in scratch.iterdir() if p.is_dir() and ".tmp." not in p.name
    )
    assert versions == ["v2"]  # v1 reaped once v2 materialised


def test_reaper_keeps_in_use_versions(tmp_path: Path) -> None:
    from ietf_llm.corpus_store import pin_corpus_version

    store = _cloud(tmp_path)
    for ver in ("v1", "v2"):
        (tmp_path / "scratch" / "tls" / ver / "files").mkdir(parents=True)
    # v1 pinned by an in-flight request -> the reaper must keep it.
    with pin_corpus_version("tls", "v1"):
        store._reap_scratch("tls", "v2")
        assert (tmp_path / "scratch" / "tls" / "v1").is_dir()
    # Unpinned -> the next reap removes it, keeping the current version.
    store._reap_scratch("tls", "v2")
    assert not (tmp_path / "scratch" / "tls" / "v1").exists()
    assert (tmp_path / "scratch" / "tls" / "v2").is_dir()


def test_reaper_skips_tmp_staging_dirs(tmp_path: Path) -> None:
    store = _cloud(tmp_path)
    (tmp_path / "scratch" / "tls" / "v1" / "files").mkdir(parents=True)
    (tmp_path / "scratch" / "tls" / "v2.tmp.deadbeef").mkdir(parents=True)
    store._reap_scratch("tls", "v1")  # current is v1
    # An in-progress / crashed staging dir is never touched by the reaper.
    assert (tmp_path / "scratch" / "tls" / "v2.tmp.deadbeef").is_dir()
