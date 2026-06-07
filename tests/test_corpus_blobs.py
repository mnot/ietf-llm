"""Tests for the file:// blob store."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.corpus_blobs import FileBlobStore


def test_put_get_exists_roundtrip(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    assert store.exists("tls/v1/a.md") is False
    store.put("tls/v1/a.md", b"hello")
    assert store.exists("tls/v1/a.md") is True
    assert store.get("tls/v1/a.md") == b"hello"


def test_put_leaves_no_tmp_file(tmp_path: Path) -> None:
    base = tmp_path / "bucket"
    store = FileBlobStore(str(base))
    store.put("tls/v1/a.md", b"x")
    assert [p.name for p in base.rglob("*.tmp")] == []


def test_unsafe_keys_rejected(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    for bad in ("/abs", "a/../b", "", "a/./b", "a//b"):
        with pytest.raises(ValueError):
            store.put(bad, b"x")


def test_list_prefix(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    store.put("tls/v1/a.md", b"1")
    store.put("tls/v1/sub/b.md", b"2")
    store.put("tls/v2/c.md", b"3")
    assert store.list_prefix("tls/v1") == ["tls/v1/a.md", "tls/v1/sub/b.md"]
    assert store.list_prefix("nope") == []


def test_materialise_prefix(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    store.put("tls/v1/files/digests/index.md", b"idx")
    store.put("tls/v1/files/group.md", b"grp")
    dest = tmp_path / "scratch"
    store.materialise_prefix("tls/v1/files/", str(dest))
    assert (dest / "digests" / "index.md").read_bytes() == b"idx"
    assert (dest / "group.md").read_bytes() == b"grp"


def test_delete_prefix(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    store.put("tls/v1/a.md", b"1")
    store.put("tls/v1/sub/b.md", b"2")
    store.put("tls/v2/c.md", b"3")
    store.delete_prefix("tls/v1/")
    # Only v1's tree is gone; v2 is untouched.
    assert store.list_prefix("tls/v1") == []
    assert store.exists("tls/v1/a.md") is False
    assert store.exists("tls/v2/c.md") is True


def test_delete_prefix_absent_is_noop(tmp_path: Path) -> None:
    store = FileBlobStore(str(tmp_path / "bucket"))
    # Nothing stored under the prefix — must not raise.
    store.delete_prefix("tls/ghost/")
