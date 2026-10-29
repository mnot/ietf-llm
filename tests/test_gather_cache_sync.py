"""Tests for the cloud gather-cache sync (ietf_llm.gather.sources.cache_sync) and the
CorpusStore hydrate/persist seam (issue #82).

The sync round-trips the gather accelerator caches to the KvStore so an
ephemeral host doesn't re-hit rate-limited upstreams after scale-to-zero:
the ETag store per corpus (lease-serialised, plain RMW) and the shared
identity maps (CAS-merge). Tests drive the real cache-module path helpers
through an InMemoryKvStore, simulating a cold start by switching the cache dir.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
from pathlib import Path
from typing import List, Optional

import pytest

from ietf_llm.singletons.catalog import catalog_index_dir
from ietf_llm.store.corpus import GatherCacheRestoreError, LocalCorpusStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.gather.sources import (
    cache_sync,
    datatracker,
    datatracker_github,
    datatracker_people,
    github_users,
    http_cache,
)
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import ANY, InMemoryKvStore, KvStore, Record


@pytest.fixture(autouse=True)
def _reset_http_caches():
    # The ETag store is per-thread (bound) + a lazy process-default; reset both
    # around each test so a binding / cache dir never leaks between tests.
    datatracker.bind_gather_corpus(None)
    datatracker._DEFAULT_CACHE = None
    yield
    datatracker.bind_gather_corpus(None)
    datatracker._DEFAULT_CACHE = None


def _gather_stores(corpus: str, url: str, etag: str, body: str = "{}") -> None:
    """Stand in for a gather that fetched `url`: the gather-time ETag binding,
    flushed to the per-corpus file when it ends."""
    with datatracker.gather_http_cache(corpus):
        datatracker._active_http_cache().store(url, etag, body)


def _cache_dir(monkeypatch, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(path))
    datatracker.bind_gather_corpus(None)
    datatracker._DEFAULT_CACHE = None  # rebuild from the new dir on next access
    return path


# --- http cache: per-corpus round-trip ------------------------------------

_HTTP_KEY = "corpora/{}/gather-cache/http-cache.tar"
_LEGACY_HTTP_KEY = "corpora/{}/gather-cache/http-cache.json"


def _stored_http_cache(kv: KvStore, corpus: str) -> dict:
    """The stored ETag store, unpacked: `{member name: bytes}`."""
    record = kv.get(_HTTP_KEY.format(corpus))
    assert record is not None
    members = {}
    with tarfile.open(fileobj=io.BytesIO(record[0]), mode="r:") as tar:
        for member in tar:
            handle = tar.extractfile(member)
            assert handle is not None
            members[member.name] = handle.read()
    return members


def _stored_urls(kv: KvStore, corpus: str) -> set:
    return set(json.loads(_stored_http_cache(kv, corpus)["index.json"])["entries"])


def _tar(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_http_cache_roundtrip_across_a_cold_start(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    url = "https://datatracker.ietf.org/api/v1/group/?format=json"
    _cache_dir(monkeypatch, tmp_path / "warm")
    _gather_stores("tls", url, "etag-1", '{"hit": true}')
    cache_sync.persist(kv, "tls")  # uploads what the gather left on disk

    # Cold host: fresh, empty cache dir; nothing local.
    _cache_dir(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "tls")  # restores the per-corpus file; binds nothing

    assert os.path.exists(datatracker.http_cache_path("tls"))
    assert datatracker._active_http_cache() is datatracker._default_http_cache()
    with datatracker.gather_http_cache("tls"):
        restored = datatracker._active_http_cache().get(url)
    assert restored is not None and restored["etag"] == "etag-1"


def test_http_cache_is_sharded_per_corpus(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "a")
    _gather_stores("alpha", "u-a", "etag-a")
    cache_sync.persist(kv, "alpha")

    _cache_dir(monkeypatch, tmp_path / "b")
    _gather_stores("beta", "u-b", "etag-b")
    cache_sync.persist(kv, "beta")

    # Each corpus has its own key; beta's gather did not clobber alpha's delta.
    assert _stored_urls(kv, "alpha") == {"u-a"}
    assert _stored_urls(kv, "beta") == {"u-b"}


def test_concurrent_threads_get_isolated_http_caches(monkeypatch, tmp_path):
    # The runner gathers up to N corpora concurrently, one per worker thread.
    # Binding the ETag store per thread must keep two overlapping gathers off one
    # shared in-memory store (the bug #1 in the review).
    _cache_dir(monkeypatch, tmp_path / "host")
    started = threading.Barrier(2)
    seen = {}

    def run(corpus, mine, theirs):
        datatracker.bind_gather_corpus(corpus)
        cache = datatracker._active_http_cache()
        cache.store(mine, "e", "{}")
        started.wait()  # both have stored before either inspects
        seen[corpus] = (cache.get(mine) is not None, cache.get(theirs) is not None)

    t1 = threading.Thread(target=run, args=("alpha", "u-alpha", "u-beta"))
    t2 = threading.Thread(target=run, args=("beta", "u-beta", "u-alpha"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert seen["alpha"] == (True, False)
    assert seen["beta"] == (True, False)


def test_the_stored_bodies_survive_the_round_trip(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    url = "https://datatracker.ietf.org/api/v1/group/?format=json"
    _cache_dir(monkeypatch, tmp_path / "warm")
    _gather_stores("tls", url, "etag-1", '{"hit": true}')
    cache_sync.persist(kv, "tls")
    first = _stored_http_cache(kv, "tls")

    _cache_dir(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "tls")
    with datatracker.gather_http_cache("tls"):
        assert datatracker._active_http_cache().body(url) == '{"hit": true}'
        datatracker._active_http_cache().get(url)  # a revalidation touches it
    cache_sync.persist(kv, "tls")
    # The same body files; only the index (last_used) differs.
    second = _stored_http_cache(kv, "tls")
    assert second.keys() == first.keys()
    assert {n: d for n, d in second.items() if n != "index.json"} == {
        n: d for n, d in first.items() if n != "index.json"
    }


def test_persist_with_no_local_store_leaves_the_stored_one(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "warm")
    _gather_stores("tls", "u", "e")
    cache_sync.persist(kv, "tls")
    stored = kv.get(_HTTP_KEY.format("tls"))

    _cache_dir(monkeypatch, tmp_path / "cold")  # a gather that never fetched
    cache_sync.persist(kv, "tls")
    assert kv.get(_HTTP_KEY.format("tls"))[0] == stored[0]


def test_a_blob_stored_before_the_directory_layout_is_migrated(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    url = "https://datatracker.ietf.org/api/v1/group/?format=json"
    legacy = {url: {"etag": "old", "body": '{"was": "blob"}', "last_used": 1.0e9}}
    kv.put(_LEGACY_HTTP_KEY.format("tls"), json.dumps(legacy).encode())
    _cache_dir(monkeypatch, tmp_path / "cold")

    cache_sync.hydrate(kv, "tls")
    with datatracker.gather_http_cache("tls"):
        cache = datatracker._active_http_cache()
        assert cache.get(url)["etag"] == "old"
        assert cache.body(url) == '{"was": "blob"}'
    cache_sync.persist(kv, "tls")

    assert _stored_urls(kv, "tls") == {url}
    assert kv.get(_LEGACY_HTTP_KEY.format("tls")) is None  # superseded


@pytest.mark.parametrize("blob", [b"", b"{truncated", b"[1, 2]", b'{"u": 5}'])
def test_a_corrupt_stored_blob_does_not_fail_the_gather(monkeypatch, tmp_path, blob):
    kv = InMemoryKvStore()
    kv.put(_LEGACY_HTTP_KEY.format("tls"), blob)
    _cache_dir(monkeypatch, tmp_path / "cold")

    assert cache_sync.hydrate(kv, "tls") == []
    _gather_stores("tls", "u", "e")  # the gather starts from nothing
    cache_sync.persist(kv, "tls")
    assert _stored_urls(kv, "tls") == {"u"}


def test_a_corrupt_stored_archive_does_not_fail_the_gather(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    kv.put(_HTTP_KEY.format("tls"), b"this is not a tar")
    _cache_dir(monkeypatch, tmp_path / "cold")

    assert cache_sync.hydrate(kv, "tls") == []
    _gather_stores("tls", "u", "e")
    cache_sync.persist(kv, "tls")
    assert _stored_urls(kv, "tls") == {"u"}  # replaced by what the gather learned


def test_a_hostile_archive_writes_only_store_files(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    name = "a" * 64 + ".gz"
    index = {"version": 1, "entries": {"u": {"etag": "e", "file": name, "size": 1}}}
    kv.put(
        _HTTP_KEY.format("tls"),
        _tar(
            {
                "../../escape.txt": b"x",
                "/abs.txt": b"x",
                "notes.txt": b"x",
                name: b"body",
                "index.json": json.dumps(index).encode(),
            }
        ),
    )
    _cache_dir(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "tls")

    store = Path(datatracker.http_cache_path("tls"))
    assert sorted(p.name for p in store.iterdir()) == sorted(["index.json", name])
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "cold" / "escape.txt").exists()


def test_a_truncated_archive_leaves_the_local_index_alone(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "warm")
    _gather_stores("tls", "stored-url", "e")
    cache_sync.persist(kv, "tls")
    whole = kv.get(_HTTP_KEY.format("tls"))[0]

    _cache_dir(monkeypatch, tmp_path / "other")
    _gather_stores("tls", "other-url", "e2")
    # One body (header + a block of data), then the index cut off mid-data.
    kv.put(_HTTP_KEY.format("tls"), whole[: 1024 + 512 + 20])
    cache_sync.hydrate(kv, "tls")

    index = http_cache.read_index(datatracker.http_cache_path("tls"))
    assert index is not None and set(index) == {"other-url"}


def test_hydrate_keeps_a_local_store_over_a_stored_blob(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    kv.put(
        _LEGACY_HTTP_KEY.format("tls"),
        json.dumps({"stored": {"etag": "e", "body": "{}"}}).encode(),
    )
    _cache_dir(monkeypatch, tmp_path / "warm")
    _gather_stores("tls", "local", "e")  # this host already has a store
    cache_sync.hydrate(kv, "tls")
    index = http_cache.read_index(datatracker.http_cache_path("tls"))
    assert index is not None and set(index) == {"local"}
    assert not os.path.exists(datatracker.legacy_http_cache_path("tls"))


# --- identity maps: shared CAS-merge --------------------------------------


def test_identity_map_merge_is_lossless_under_concurrent_writes(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    key = "fleet/gather-cache/github-users.json"
    # A prior fleet gather already stored alice.
    kv.put(key, json.dumps({"alice": {"name": "Alice", "fetched_at": "2026-01-01"}}).encode())

    _cache_dir(monkeypatch, tmp_path / "host")
    cache_sync.hydrate(kv, "tls")  # local now has alice
    # This gather resolves bob (append to the local file via the real saver).
    local = github_users._load_cache()
    local["bob"] = {"name": "Bob", "fetched_at": "2026-02-02"}
    github_users._save_cache(local)
    # Meanwhile a concurrent host adds carol to the shared key.
    kv.put(
        key,
        json.dumps(
            {
                "alice": {"name": "Alice", "fetched_at": "2026-01-01"},
                "carol": {"name": "Carol", "fetched_at": "2026-03-03"},
            }
        ).encode(),
    )

    cache_sync.persist(kv, "tls")

    merged = json.loads(kv.get(key)[0])
    assert set(merged) == {"alice", "bob", "carol"}  # no delta lost


def test_local_merge_save_reloads_before_writing(monkeypatch, tmp_path):
    # #3: a concurrent same-process gather's local additions must not be clobbered
    # by another gather's load-modify-save. _merge_save reloads the current file
    # and merges, rather than overwriting with a stale in-memory snapshot.
    _cache_dir(monkeypatch, tmp_path / "host")
    # Gather A landed alice on disk after gather B took its empty start-snapshot.
    github_users._save_cache({"alice": {"name": "A", "fetched_at": "2026-01-01"}})
    # Gather B persists only its own learned bob.
    github_users._merge_save({"bob": {"name": "B", "fetched_at": "2026-02-02"}})
    assert set(github_users._load_cache()) == {"alice", "bob"}


def test_datatracker_github_merge_save_preserves_concurrent_index(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    datatracker_github._save_cache(
        {"_index": {"a": "/p/1"}, "_index_fetched_at": "2026-05-05", "a": None}
    )
    # An older in-memory snapshot must not overwrite the newer on-disk index.
    datatracker_github._merge_save(
        {"_index": {}, "_index_fetched_at": "2026-01-01", "b": None}
    )
    on_disk = datatracker_github._load_cache()
    assert on_disk["_index"] == {"a": "/p/1"}  # newer index kept
    assert "a" in on_disk and "b" in on_disk  # both per-login misses retained


def test_github_users_merge_prefers_newer_fetched_at():
    remote = {"x": {"name": None, "fetched_at": "2026-01-01"}}
    local = {"x": {"name": "Real", "fetched_at": "2026-05-05"}}
    merged = github_users.merge_cache(remote, local)
    assert merged["x"]["name"] == "Real"


def test_datatracker_people_merge_prefers_hit_over_none_miss():
    # A real resolution (stamped) must beat a bare `None` miss on merge, and
    # disjoint addresses from concurrent gathers must both survive.
    remote = {"a@x": None}
    local = {"a@x": {"person": "/p/1", "fetched_at": "2026-06-06"}, "b@y": None}
    merged = datatracker_people.merge_cache(remote, local)
    assert merged["a@x"] == {"person": "/p/1", "fetched_at": "2026-06-06"}
    assert merged["b@y"] is None


def test_datatracker_people_roundtrip_across_a_cold_start(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "warm")
    datatracker_people._save_cache({"mnot@mnot.net": {"person": "/p/1", "fetched_at": "x"}})
    cache_sync.persist(kv, "tls")
    # Cold host: fresh cache dir, then hydrate restores the shared map.
    _cache_dir(monkeypatch, tmp_path / "cold")
    assert datatracker_people._load_cache() == {}
    cache_sync.hydrate(kv, "tls")
    assert datatracker_people._load_cache()["mnot@mnot.net"]["person"] == "/p/1"


def test_datatracker_github_merge_keeps_index_and_none_misses():
    remote = {
        "_index": {"a": "/p/1"},
        "_index_fetched_at": "2026-01-01",
        "a": {"name": "A", "emails": [], "fetched_at": "2026-01-01"},
    }
    local = {
        "_index": {"a": "/p/1", "b": "/p/2"},
        "_index_fetched_at": "2026-06-06",
        "b": None,  # confirmed not on datatracker
    }
    merged = datatracker_github.merge_cache(remote, local)
    assert merged["_index_fetched_at"] == "2026-06-06"  # newer index wins
    assert merged["_index"] == {"a": "/p/1", "b": "/p/2"}
    assert merged["a"]["name"] == "A"  # remote-only per-login survives
    assert merged["b"] is None  # None miss preserved


# --- catalog: shared fleet directory singleton ----------------------------


def test_catalog_dir_roundtrip_across_a_cold_start(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "warm")
    cat = catalog_index_dir()  # <warm>/_catalog
    os.makedirs(cat, exist_ok=True)
    (Path(cat) / "catalog.json").write_text('[{"acronym": "tls"}]')
    (Path(cat) / "raw-active.json").write_text('{"objects": []}')
    (Path(cat) / "raw-active.json.etag").write_text('"abc"')
    cache_sync.persist(kv, "tls")

    # Every catalog file landed under the shared fleet prefix (last-writer-wins).
    assert kv.get("fleet/catalog/catalog.json") is not None
    assert kv.get("fleet/catalog/raw-active.json.etag") is not None

    # Cold host: empty cache dir, no _catalog -> hydrate restores the tree.
    _cache_dir(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "tls")
    restored = Path(catalog_index_dir())
    assert (restored / "catalog.json").read_text() == '[{"acronym": "tls"}]'
    assert (restored / "raw-active.json.etag").read_text() == '"abc"'


# --- best-effort: failures never propagate --------------------------------


class _RaisingKv(KvStore):
    def get(self, key: str) -> Optional[Record]:
        raise RuntimeError("boom")

    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        raise RuntimeError("boom")

    def delete(self, key: str) -> None:
        raise RuntimeError("boom")

    def list_children(self, prefix: str) -> List[str]:
        raise RuntimeError("boom")


def test_sync_swallows_kvstore_errors(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    _gather_stores("tls", "u", "e")
    # A broken store never fails persist, nor hydrate of the accelerator caches;
    # the one thing hydrate raises for is stored IMAP mail it couldn't list or
    # fetch (see test_imap_cache_sync).
    cache_sync.persist(_RaisingKv(), "tls")
    with pytest.raises(GatherCacheRestoreError, match="listing failed"):
        cache_sync.hydrate(_RaisingKv(), "tls")


# --- seam: local no-op, cloud delegates -----------------------------------


def test_local_backend_seam_is_noop(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    store = LocalCorpusStore()
    # No KvStore, no network, no exception — just nothing.
    store.hydrate_gather_caches("tls")
    store.persist_gather_caches("tls")


def test_cloud_seam_delegates_to_sync(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    store = CloudCorpusStore(
        KvControlPlane(kv),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
        kv=kv,
    )
    _cache_dir(monkeypatch, tmp_path / "host")
    _gather_stores("tls", "u", "e")
    store.persist_gather_caches("tls")
    assert kv.get(_HTTP_KEY.format("tls")) is not None


def test_cloud_seam_without_kv_is_noop(tmp_path):
    store = CloudCorpusStore(
        KvControlPlane(InMemoryKvStore()),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
    )
    store.hydrate_gather_caches("tls")  # kv is None -> no-op, no raise
    store.persist_gather_caches("tls")


# --- the gather-time half: binding and flushing the ETag store ---------------


def _run_gather_one(monkeypatch, corpus: str, backend: str, seen: dict):
    """Drive the real `_gather_one` wrapper with a stage body that records which
    ETag store is active and fetches one URL through it."""
    from argparse import Namespace

    from ietf_llm.gather import sequencer
    from ietf_llm.log import Verbosity

    def stages(args, verbosity, progress, note_fn, pre):
        seen["store"] = datatracker._active_http_cache()
        seen["store"].store("u", "etag-1", "{}")
        return sequencer.GatherOutcome.GATHERED

    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", backend)
    monkeypatch.setattr(
        sequencer, "preflight", lambda *a, **k: sequencer.Preflight(None, {}, False, False)
    )
    monkeypatch.setattr(sequencer, "_gather_stages", stages)
    return sequencer._gather_one(Namespace(wg=corpus), Verbosity.QUIET)


def test_a_cloud_gather_binds_and_flushes_the_corpus_etag_file(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    seen: dict = {}
    _run_gather_one(monkeypatch, "tls", "cloud", seen)

    assert seen["store"] is not datatracker._default_http_cache()
    on_disk = http_cache.read_index(datatracker.http_cache_path("tls"))
    assert on_disk is not None and on_disk["u"]["etag"] == "etag-1"
    # Unbound again, so the next gather on this reused thread starts clean.
    assert datatracker._active_http_cache() is datatracker._default_http_cache()


def test_a_local_gather_leaves_the_default_etag_store_alone(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    seen: dict = {}
    _run_gather_one(monkeypatch, "tls", "local", seen)

    assert seen["store"] is datatracker._default_http_cache()
    assert not os.path.exists(datatracker.http_cache_path("tls"))


def test_the_etag_file_is_flushed_when_the_stages_raise(monkeypatch, tmp_path):
    _cache_dir(monkeypatch, tmp_path / "host")
    with pytest.raises(RuntimeError):
        with datatracker.gather_http_cache("tls"):
            datatracker._active_http_cache().store("u", "etag-1", "{}")
            raise RuntimeError("stage blew up")
    assert "u" in (http_cache.read_index(datatracker.http_cache_path("tls")) or {})
    assert datatracker._active_http_cache() is datatracker._default_http_cache()
