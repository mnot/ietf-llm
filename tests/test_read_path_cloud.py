"""Feature 4: the MCP read path resolves and materialises through the corpus
store, so the same read tools serve a cloud (sqlite + file://) backend — not
just the local cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import mcp_server
from ietf_llm.corpus_blobs import FileBlobStore
from ietf_llm.corpus_control import SqliteControlPlane
from ietf_llm.corpus_store_cloud import CloudCorpusStore


def _cloud_store(root: Path) -> CloudCorpusStore:
    control = SqliteControlPlane(str(root / "control.db"))
    blobs = FileBlobStore(str(root / "bucket"))
    return CloudCorpusStore(control, blobs, str(root / "scratch"))


def _publish_tls(store: CloudCorpusStore, root: Path) -> None:
    ws = root / "ws"
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text("# Overview\n")
    (ws / "files" / "threads").mkdir(parents=True)
    (ws / "files" / "threads" / "2026-06-01-hello.md").write_text("hi")
    store.publish("tls", str(ws), version="v1")


def test_read_tools_serve_cloud_backend(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = isolated_home / "cloud"
    store = _cloud_store(root)
    _publish_tls(store, root)
    monkeypatch.setattr(mcp_server, "get_corpus_store", lambda: store)

    # Existence + enumeration resolve through the cloud control plane...
    assert mcp_server._corpus_exists("tls") is True
    assert mcp_server._list_wgs() == ["tls"]
    # ...and a read tool materialises the version onto scratch and lists its
    # published files through the existing path helpers.
    out = mcp_server.tool_list_files("tls")
    assert "digests/index.md" in out
    assert "threads/2026-06-01-hello.md" in out


def test_files_dir_raises_without_current_version(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty cloud store: no corpus has a current version. _files_dir
    # surfaces the error rather than fabricating a path (the _requires_corpus
    # guard normally prevents a tool from ever reaching it for an absent corpus).
    store = _cloud_store(isolated_home / "empty")
    monkeypatch.setattr(mcp_server, "get_corpus_store", lambda: store)
    assert mcp_server._corpus_exists("ghost") is False
    with pytest.raises(FileNotFoundError):
        mcp_server._files_dir("ghost")
