"""Tests for the concurrency / correctness hardening from the PR #68 review
(findings G-1..G-10)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ietf_llm import corpus_control
from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.corpus_control import SqliteControlPlane
from ietf_llm.corpus_store import LocalCorpusStore, get_corpus_store
from ietf_llm.corpus_store_cloud import CloudCorpusStore
from ietf_llm.gather_runner import _owner

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_CONTROL_DB",
    "IETF_LLM_BLOB_DIR",
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
        SqliteControlPlane(str(tmp_path / "c.db")),
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
    (tmp_path / "bucket" / "tls" / "v1" / "files" / "digests" / "index.md").unlink()
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
