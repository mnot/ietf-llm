"""Feature 4: the MCP read path resolves and materialises through the corpus
store, so the same read tools serve a cloud backend (in-memory control plane +
file:// blobs here) — not just the local cache."""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path

import pytest

from ietf_llm.gather.sources import documents_manifest
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.cloud import CloudCorpusStore, _clear_resolve_cache
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore


def _cloud_store(root: Path) -> CloudCorpusStore:
    control = KvControlPlane(InMemoryKvStore())
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
    monkeypatch.setattr(mcp.common, "get_corpus_store", lambda: store)

    # Existence + enumeration resolve through the cloud control plane...
    assert mcp.common._corpus_exists("tls") is True
    assert mcp.common._list_wgs() == ["tls"]
    # ...and a read tool materialises the version onto scratch and lists its
    # published files through the existing path helpers.
    out = mcp.corpus.tool_list_files("tls")
    assert "digests/index.md" in out
    assert "threads/2026-06-01-hello.md" in out


def test_list_drafts_reads_the_published_documents_manifest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`documents.json` sits in the corpus *root*, so it rides along in a
    published version — but it materialises into per-version scratch, not into
    `<cache>/<corpus>/`. Composing its path from the cache root made every
    lifecycle reader on the cloud backend report "nothing recorded" for a corpus
    that had a perfectly good manifest."""
    root = isolated_home / "cloud"
    store = _cloud_store(root)
    ws = root / "ws"
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text("# Overview\n")
    (ws / "documents.json").write_text(
        '{"draft-ietf-tls-esni": {"expires": "2099-01-01T00:00:00Z",'
        ' "state": "active"},'
        ' "draft-ietf-tls-rfc8446bis": {"expires": "", "state": "rfc"}}'
    )
    store.publish("tls", str(ws), version="v1")
    monkeypatch.setattr(mcp.common, "get_corpus_store", lambda: store)
    monkeypatch.setattr(documents_manifest, "get_corpus_store", lambda: store)

    out = mcp.drafts.tool_list_drafts("tls")
    assert "No draft lifecycle state recorded" not in out
    assert "draft-ietf-tls-esni" in out and "active I-D" in out
    assert "draft-ietf-tls-rfc8446bis" in out and "published RFC" in out
    # The state filter reads the same manifest.
    filtered = mcp.drafts.tool_list_drafts("tls", state="rfc")
    assert "draft-ietf-tls-rfc8446bis" in filtered
    assert "draft-ietf-tls-esni" not in filtered


def _publish_version(store: CloudCorpusStore, root: Path, version: str, body: str) -> None:
    ws = root / f"ws-{version}"
    (ws / "files" / "digests").mkdir(parents=True)
    (ws / "files" / "digests" / "index.md").write_text("# Overview\n")
    (ws / "files" / "threads").mkdir(parents=True)
    (ws / "files" / "threads" / f"{version}.md").write_text(body)
    store.publish("tls", str(ws), version=version)


def test_tool_call_retries_when_pinned_version_is_reaped(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two replicas over one control plane + bucket: the reader caches v1 as
    # current (TTL 100s) while the writer publishes v2 and reaps v1's blobs.
    # The tool wrapper pins the stale v1, the read raises VersionVanished, and
    # the wrapper re-runs the whole call once on the now-current v2.
    _clear_resolve_cache()
    root = isolated_home / "cloud"
    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(root / "bucket"))
    reader = CloudCorpusStore(
        control, blobs, str(root / "scratch-r"), resolve_ttl=100.0, cache_key="r"
    )
    writer = CloudCorpusStore(
        control, blobs, str(root / "scratch-w"), retain_versions=1, cache_key="w"
    )
    _publish_version(reader, root, "v1", "from v1")
    _publish_version(writer, root, "v2", "from v2")
    monkeypatch.setattr(mcp.common, "get_corpus_store", lambda: reader)

    out = mcp.corpus.tool_list_files("tls")
    # The retry served v2: its thread file is listed, v1's is gone.
    assert "threads/v2.md" in out
    assert "threads/v1.md" not in out


def test_files_dir_raises_without_current_version(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty cloud store: no corpus has a current version. _files_dir
    # surfaces the error rather than fabricating a path (the _requires_corpus
    # guard normally prevents a tool from ever reaching it for an absent corpus).
    store = _cloud_store(isolated_home / "empty")
    monkeypatch.setattr(mcp.common, "get_corpus_store", lambda: store)
    assert mcp.common._corpus_exists("ghost") is False
    with pytest.raises(FileNotFoundError):
        mcp.common._files_dir("ghost")
