"""IMAP mail-cache round trip through the cloud KvStore (issue #260) and the
UIDVALIDITY guard in the IMAP sync.

A fresh cloud container starts with an empty `imap-cache/`; without a restore it
re-downloads the whole archive, and thread reconstruction (which re-reads every
cached message and deletes thread files it doesn't regenerate) needs all of it
back, not just the new mail. The mail travels as gzip-tar chunks of consecutive
UIDs plus a per-list index, through an InMemoryKvStore; a cold start is
simulated by pointing the cache dir somewhere empty.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from ietf_llm.gather.sources import cache_sync, mbox
from ietf_llm.log import Verbosity
from ietf_llm.store.kv import ANY, InMemoryKvStore, Record

CHUNK = cache_sync._IMAP_CHUNK_UIDS
PREFIX = "corpora/moq/gather-cache/imap/moq/"


class _CountingKv(InMemoryKvStore):
    """Records the keys touched, so tests can assert what a sync cost."""

    def __init__(self) -> None:
        super().__init__()
        self.gets: List[str] = []
        self.puts: List[str] = []

    def get(self, key: str) -> Optional[Record]:
        self.gets.append(key)
        return super().get(key)

    def put(self, key: str, value: bytes, *, expect: object = ANY) -> Optional[str]:
        self.puts.append(key)
        return super().put(key, value, expect=expect)

    def reset(self) -> None:
        self.gets.clear()
        self.puts.clear()


def _host(monkeypatch, path: Path) -> Path:
    """Point the cache dir at `path` (a fresh host) and return the list dir."""
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(path))
    list_dir = path / "imap-cache" / "moq" / "moq"
    list_dir.mkdir(parents=True, exist_ok=True)
    return list_dir


def _mail(list_dir: Path, uids, epoch: Optional[str] = "7") -> None:
    for uid in uids:
        (list_dir / f"{uid}.eml").write_bytes(f"Message-Id: <{uid}@x>\n\nbody {uid}\n".encode())
    if epoch is not None:
        (list_dir / mbox.UIDVALIDITY_FILE).write_text(epoch)


def _snapshot(list_dir: Path) -> Dict[str, bytes]:
    return {p.name: p.read_bytes() for p in list_dir.iterdir()}


def _chunk_puts(kv: _CountingKv) -> List[str]:
    return [k for k in kv.puts if "chunk-" in k]


# --- round trip ------------------------------------------------------------


def test_cold_start_restores_all_mail_from_few_objects(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, range(1, 3 * CHUNK + 100, 3))  # spans several chunks
    cache_sync.persist(kv, "moq")
    expected = _snapshot(warm)

    # One object per chunk plus the index — not one per message.
    n_chunks = len({u // CHUNK for u in range(1, 3 * CHUNK + 100, 3)})
    assert len(_chunk_puts(kv)) == n_chunks
    assert len(kv.puts) == n_chunks + 1

    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert _snapshot(cold) == expected  # mail and the UIDVALIDITY marker


def test_warm_host_fetches_no_chunks(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, range(1, 2 * CHUNK))
    cache_sync.persist(kv, "moq")
    kv.reset()

    cache_sync.hydrate(kv, "moq")  # same host, scratch intact
    assert not [k for k in kv.gets if "chunk-" in k]


def test_hydrate_fills_only_what_is_missing(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, range(1, 2 * CHUNK))
    cache_sync.persist(kv, "moq")
    kv.reset()

    (warm / "5.eml").unlink()
    cache_sync.hydrate(kv, "moq")
    assert (warm / "5.eml").exists()
    assert len([k for k in kv.gets if "chunk-" in k]) == 1  # just chunk 0


def test_persist_uploads_only_the_changed_chunk(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, range(1, 2 * CHUNK))
    cache_sync.persist(kv, "moq")
    kv.reset()

    _mail(warm, [2 * CHUNK + 1])  # new mail lands in a fresh tail chunk
    cache_sync.persist(kv, "moq")
    assert _chunk_puts(kv) == [PREFIX + "chunk-2.tar.gz"]

    kv.reset()
    cache_sync.persist(kv, "moq")  # nothing changed
    assert kv.puts == []


def test_wider_window_refreshes_an_older_chunk(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, range(CHUNK, 2 * CHUNK))
    cache_sync.persist(kv, "moq")
    kv.reset()

    _mail(warm, [3, 4])  # a wider --months fills in a long-closed chunk
    cache_sync.persist(kv, "moq")
    assert _chunk_puts(kv) == [PREFIX + "chunk-0.tar.gz"]

    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert (cold / "3.eml").exists() and (cold / f"{CHUNK}.eml").exists()


# --- never lose stored mail -------------------------------------------------


def test_persist_never_overwrites_a_chunk_with_less(monkeypatch, tmp_path):
    kv = _CountingKv()
    full = _host(monkeypatch, tmp_path / "full")
    _mail(full, [1, 2, 3, 4])
    cache_sync.persist(kv, "moq")

    # A host whose hydrate failed has only part of the chunk, plus new mail.
    partial = _host(monkeypatch, tmp_path / "partial")
    _mail(partial, [4, 5])
    cache_sync.persist(kv, "moq")

    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert sorted(p.name for p in cold.glob("*.eml")) == [f"{u}.eml" for u in range(1, 6)]


def test_failed_stored_chunk_fetch_leaves_the_store_alone(monkeypatch, tmp_path):
    kv = _CountingKv()
    full = _host(monkeypatch, tmp_path / "full")
    _mail(full, [1, 2, 3])
    cache_sync.persist(kv, "moq")
    before = dict(kv._data)

    partial = _host(monkeypatch, tmp_path / "partial")
    _mail(partial, [3])

    def boom(key: str) -> Optional[Record]:
        if "chunk-" in key:
            raise OSError("bucket unreachable")
        return InMemoryKvStore.get(kv, key)

    monkeypatch.setattr(kv, "get", boom)
    cache_sync.persist(kv, "moq")  # must not raise
    assert kv._data == before


def test_corrupt_index_skips_the_list(monkeypatch, tmp_path):
    kv = _CountingKv()
    kv.put(PREFIX + "index.json", b"not json")
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])
    kv.reset()

    cache_sync.persist(kv, "moq")  # must not raise
    assert kv.puts == []
    cache_sync.hydrate(kv, "moq")  # nor here
    assert sorted(p.name for p in warm.glob("*.eml")) == ["1.eml", "2.eml"]


def test_store_errors_never_fail_a_sync(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])

    def boom(*_a, **_k):
        raise OSError("down")

    monkeypatch.setattr(kv, "put", boom)
    monkeypatch.setattr(kv, "get", boom)
    monkeypatch.setattr(kv, "list_children", boom)
    cache_sync.persist(kv, "moq")
    cache_sync.hydrate(kv, "moq")


# --- UIDVALIDITY epoch ------------------------------------------------------


def test_new_epoch_replaces_the_stored_mail(monkeypatch, tmp_path):
    kv = _CountingKv()
    old = _host(monkeypatch, tmp_path / "old")
    _mail(old, [1, 2, 3, CHUNK + 1], epoch="7")
    cache_sync.persist(kv, "moq")

    # The server renumbered the folder; the sync wiped and refetched under "8".
    new = _host(monkeypatch, tmp_path / "new")
    _mail(new, [10, 11], epoch="8")
    cache_sync.persist(kv, "moq")

    assert PREFIX + "chunk-1.tar.gz" not in kv._data  # stale chunk deleted
    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert sorted(p.name for p in cold.glob("*.eml")) == ["10.eml", "11.eml"]
    assert (cold / mbox.UIDVALIDITY_FILE).read_text() == "8"


def test_hydrate_skips_a_list_cached_under_another_epoch(monkeypatch, tmp_path):
    kv = _CountingKv()
    other = _host(monkeypatch, tmp_path / "other")
    _mail(other, [1, 2], epoch="7")
    cache_sync.persist(kv, "moq")

    here = _host(monkeypatch, tmp_path / "here")
    _mail(here, [9], epoch="8")
    cache_sync.hydrate(kv, "moq")
    assert sorted(p.name for p in here.glob("*.eml")) == ["9.eml"]


def test_unmarked_local_cache_adopts_the_stored_epoch(monkeypatch, tmp_path):
    kv = _CountingKv()
    other = _host(monkeypatch, tmp_path / "other")
    _mail(other, [1], epoch="7")
    cache_sync.persist(kv, "moq")

    here = _host(monkeypatch, tmp_path / "here")
    cache_sync.hydrate(kv, "moq")
    assert (here / mbox.UIDVALIDITY_FILE).read_text() == "7"


# --- the sync's own epoch check ---------------------------------------------


class _Mail:
    def __init__(self, value: Optional[bytes]) -> None:
        self._value = value

    def response(self, name: str) -> Tuple[str, List[Optional[bytes]]]:
        assert name == "UIDVALIDITY"
        return ("UIDVALIDITY", [self._value])


def _reconcile(list_dir: Path, value: Optional[bytes]) -> None:
    mbox._reconcile_uidvalidity(_Mail(value), str(list_dir), Verbosity.QUIET)  # type: ignore[arg-type]


def test_first_sync_adopts_an_unmarked_cache(tmp_path):
    _mail(tmp_path, [1, 2], epoch=None)
    _reconcile(tmp_path, b"7")
    assert (tmp_path / "1.eml").exists()
    assert (tmp_path / mbox.UIDVALIDITY_FILE).read_text() == "7"


def test_same_epoch_keeps_the_cache(tmp_path):
    _mail(tmp_path, [1, 2], epoch="7")
    _reconcile(tmp_path, b"7")
    assert sorted(p.name for p in tmp_path.glob("*.eml")) == ["1.eml", "2.eml"]


def test_changed_epoch_clears_the_cache(tmp_path):
    _mail(tmp_path, [1, 2], epoch="7")
    _reconcile(tmp_path, b"8")
    assert list(tmp_path.glob("*.eml")) == []
    assert (tmp_path / mbox.UIDVALIDITY_FILE).read_text() == "8"


@pytest.mark.parametrize("value", [None, b"", b"garbage"])
def test_unreported_epoch_leaves_the_cache_alone(tmp_path, value):
    _mail(tmp_path, [1], epoch="7")
    _reconcile(tmp_path, value)
    assert (tmp_path / "1.eml").exists()
    assert (tmp_path / mbox.UIDVALIDITY_FILE).read_text() == "7"


def test_chunk_packing_is_deterministic():
    members = {2: b"two", 1: b"one"}
    assert cache_sync._pack_chunk(members) == cache_sync._pack_chunk(dict(members))
    assert cache_sync._unpack_chunk(cache_sync._pack_chunk(members)) == members
