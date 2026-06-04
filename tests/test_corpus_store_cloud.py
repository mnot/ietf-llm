"""Tests for the cloud CorpusStore backend (publish protocol + read path)."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from ietf_llm.corpus_blobs import BlobStore
from ietf_llm.corpus_control import ControlPlane
from ietf_llm.corpus_store_cloud import CloudCorpusStore


def _store(tmp_path: Path) -> Tuple[CloudCorpusStore, ControlPlane]:
    control = ControlPlane(str(tmp_path / "control.db"))
    blobs = BlobStore(str(tmp_path / "bucket"))
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
    control.publish_version = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.publish("tls", _workspace(tmp_path, "ws2", "second"), version="v2")

    # The prior version is still current; the staged v2 blobs are never seen.
    assert store.resolve_current("tls") == "v1"
    cache = store.local_cache_dir("tls")
    assert cache is not None
    assert (Path(cache) / "digests" / "index.md").read_text() == "first"
