"""Tests for the cloud gather-cache sync (ietf_llm.gather.cache_sync) and the
CorpusStore hydrate/persist seam (issue #82).

The sync round-trips three gather accelerator caches to the KvStore so an
ephemeral host doesn't re-hit rate-limited upstreams after scale-to-zero:
`.http-cache.json` per corpus (lease-serialised, plain RMW) and the two shared
identity maps (CAS-merge). Tests drive the real cache-module path helpers
through an InMemoryKvStore, simulating a cold start by switching the cache dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

import pytest

from ietf_llm.catalog import catalog_index_dir
from ietf_llm.corpus_store import LocalCorpusStore
from ietf_llm.corpus_store_cloud import CloudCorpusStore
from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.gather import cache_sync, datatracker, datatracker_github, github_users
from ietf_llm.kv_control import KvControlPlane
from ietf_llm.kv_store import ANY, InMemoryKvStore, KvStore, Record


@pytest.fixture(autouse=True)
def _reset_http_singleton():
    # The ETag store is a process-global singleton; reset it around each test so
    # a bound cache dir / loaded entries never leak between tests.
    datatracker.reset_http_cache()
    yield
    datatracker.reset_http_cache()


def _cache_dir(monkeypatch, path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(path))
    datatracker.reset_http_cache()  # rebind to the new dir on next access
    return path


# --- http cache: per-corpus round-trip ------------------------------------


def test_http_cache_roundtrip_across_a_cold_start(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    url = "https://datatracker.ietf.org/api/v1/group/?format=json"
    _cache_dir(monkeypatch, tmp_path / "warm")
    datatracker._HTTP_CACHE.store(url, "etag-1", '{"hit": true}')
    cache_sync.persist(kv, "tls")

    # Cold host: fresh, empty cache dir; the singleton knows nothing.
    _cache_dir(monkeypatch, tmp_path / "cold")
    assert datatracker._HTTP_CACHE.get(url) is None
    cache_sync.hydrate(kv, "tls")

    assert os.path.exists(datatracker.http_cache_path())
    restored = datatracker._HTTP_CACHE.get(url)
    assert restored is not None and restored["etag"] == "etag-1"


def test_http_cache_is_sharded_per_corpus(monkeypatch, tmp_path):
    kv = InMemoryKvStore()
    _cache_dir(monkeypatch, tmp_path / "a")
    datatracker._HTTP_CACHE.store("u-a", "etag-a", "{}")
    cache_sync.persist(kv, "alpha")

    _cache_dir(monkeypatch, tmp_path / "b")
    datatracker._HTTP_CACHE.store("u-b", "etag-b", "{}")
    cache_sync.persist(kv, "beta")

    # Each corpus has its own key; beta's gather did not clobber alpha's delta.
    alpha = kv.get("corpora/alpha/gather-cache/http-cache.json")
    assert alpha is not None and "u-a" in json.loads(alpha[0])
    beta = kv.get("corpora/beta/gather-cache/http-cache.json")
    assert beta is not None and "u-b" in json.loads(beta[0])
    assert "u-a" not in json.loads(beta[0])


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


def test_github_users_merge_prefers_newer_fetched_at():
    remote = {"x": {"name": None, "fetched_at": "2026-01-01"}}
    local = {"x": {"name": "Real", "fetched_at": "2026-05-05"}}
    merged = github_users.merge_cache(remote, local)
    assert merged["x"]["name"] == "Real"


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
    datatracker._HTTP_CACHE.store("u", "e", "{}")
    # Neither hydrate nor persist may raise on a broken store.
    cache_sync.hydrate(_RaisingKv(), "tls")
    cache_sync.persist(_RaisingKv(), "tls")


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
    datatracker._HTTP_CACHE.store("u", "e", "{}")
    store.persist_gather_caches("tls")
    assert kv.get("corpora/tls/gather-cache/http-cache.json") is not None


def test_cloud_seam_without_kv_is_noop(tmp_path):
    store = CloudCorpusStore(
        KvControlPlane(InMemoryKvStore()),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
    )
    store.hydrate_gather_caches("tls")  # kv is None -> no-op, no raise
    store.persist_gather_caches("tls")
