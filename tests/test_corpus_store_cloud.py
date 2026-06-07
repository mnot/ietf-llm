"""Tests for the cloud CorpusStore backend (publish protocol + read path)."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.corpus_store_cloud import CloudCorpusStore
from ietf_llm.kv_control import KvControlPlane
from ietf_llm.kv_store import InMemoryKvStore


def _store(tmp_path: Path) -> Tuple[CloudCorpusStore, KvControlPlane]:
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    store = CloudCorpusStore(control, blobs, str(tmp_path / "scratch"))
    return store, control


def _workspace(tmp_path: Path, name: str, index_body: str) -> str:
    ws = tmp_path / name
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text(index_body)
    (ws / "last-gathered").write_text("2026-06-04T00:00:00Z")
    return str(ws)


def test_publish_then_read_roundtrip(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    ws = _workspace(tmp_path, "ws", "hello")
    assert store.publish("tls", ws, version="v1") == "v1"
    assert store.resolve_current("tls") == "v1"
    assert store.list_corpora() == ["tls"]
    assert store.corpus_exists("tls") is True
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "hello"


def test_local_cache_dir_absent_corpus(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    assert store.resolve_current("ghost") is None
    assert store.corpus_exists("ghost") is False
    assert store.local_cache_dir("ghost") is None


def test_second_publish_moves_pointer(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "ws1", "first"), version="v1")
    store.publish("tls", _workspace(tmp_path, "ws2", "second"), version="v2")
    assert store.resolve_current("tls") == "v2"
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "second"


def test_abandoned_publish_leaves_prior_version(tmp_path: Path) -> None:
    store, control = _store(tmp_path)
    store.publish("tls", _workspace(tmp_path, "ws1", "first"), version="v1")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("pointer flip failed")

    # Simulate a crash after blobs are staged but before the pointer flips.
    control.set_current = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.publish("tls", _workspace(tmp_path, "ws2", "second"), version="v2")

    # The prior version is still current; the staged v2 blobs are never seen.
    assert store.resolve_current("tls") == "v1"
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "first"


# --- seed_workspace: pre-populate a gather workspace from the current version


def _versioned_workspace(tmp_path: Path, name: str, draft: str, db: str) -> str:
    """A workspace shaped like a real published version: a `files/` tree plus a
    top-level `embeddings.db` (the index)."""
    ws = tmp_path / name
    (ws / "files" / "drafts").mkdir(parents=True)
    (ws / "files" / "drafts" / "d.txt").write_text(draft)
    (ws / "embeddings.db").write_text(db)
    return str(ws)


def test_seed_workspace_default_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "draft", "IDX"), "v1")

    # Index dir == workspace parent: the default layout, where embeddings.db
    # belongs inside the swapped workspace.
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "tls"
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "draft"
    assert (dest / "embeddings.db").read_text() == "IDX"


def test_seed_workspace_split_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "draft", "IDX"), "v1")

    # Split index (IETF_LLM_INDEX_DIR off the cache): the DB must land where
    # build_index reads it, not in the workspace.
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "index"))
    dest = tmp_path / "cache" / "tls"
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "draft"
    assert not (dest / "embeddings.db").exists()
    assert (tmp_path / "index" / "tls" / "embeddings.db").read_text() == "IDX"


def test_seed_workspace_no_published_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "ghost"
    assert store.seed_workspace("ghost", str(dest)) is None
    assert not dest.exists()


def test_seed_workspace_replaces_stale_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    store.publish("tls", _versioned_workspace(tmp_path, "src", "v1draft", "IDX"), "v1")
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(tmp_path / "cache"))
    dest = tmp_path / "cache" / "tls"
    # A stale workspace from an older gather: a file absent in v1.
    (dest / "files" / "drafts").mkdir(parents=True)
    (dest / "files" / "drafts" / "old.txt").write_text("stale")
    assert store.seed_workspace("tls", str(dest)) == "v1"
    assert (dest / "files" / "drafts" / "d.txt").read_text() == "v1draft"
    assert not (dest / "files" / "drafts" / "old.txt").exists()
