"""Tests for the file:// blob store."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ietf_llm.corpus_blobs import FileBlobStore, parallel_each


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


def test_parallel_each_applies_to_every_item(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the concurrent path (>1 worker, >1 item) and confirm every item is
    # processed exactly once.
    monkeypatch.setenv("IETF_LLM_S3_CONCURRENCY", "4")
    seen: set[int] = set()
    lock = threading.Lock()

    def _record(item: int) -> None:
        with lock:
            seen.add(item)

    parallel_each(_record, list(range(20)))
    assert seen == set(range(20))


def test_parallel_each_reraises_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The publish/materialise safety property: any worker failure propagates,
    # so a partial upload never lets the pointer flip.
    monkeypatch.setenv("IETF_LLM_S3_CONCURRENCY", "4")

    def _boom(item: int) -> None:
        if item == 7:
            raise RuntimeError("blob lost")

    with pytest.raises(RuntimeError, match="blob lost"):
        parallel_each(_boom, list(range(20)))


def test_parallel_each_serial_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_S3_CONCURRENCY", "1")
    order: list[int] = []
    parallel_each(order.append, [3, 1, 2])
    assert order == [3, 1, 2]  # serial preserves submission order
