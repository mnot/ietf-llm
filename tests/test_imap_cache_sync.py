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
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from ietf_llm.gather.sources import cache_sync, mbox
from ietf_llm.log import Verbosity
from ietf_llm.store.corpus import GatherCacheRestoreError
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


def _verify(list_dir: Path, epoch: str) -> None:
    """Stand in for a sync that confirmed `epoch` with the server this gather."""
    mbox.record_verified(str(list_dir), epoch)


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


def _names(directory: Path) -> List[str]:
    """The `.eml` file names in `directory`, in numeric UID order."""
    return sorted((p.name for p in directory.glob("*.eml")), key=lambda n: int(n[:-4]))


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

    assert cache_sync.persist(kv, "moq")  # reported, not raised
    assert kv.puts == []
    problems = cache_sync.hydrate(kv, "moq")  # a note too, not a failure
    assert len(problems) == 1 and "unreadable index" in problems[0]
    assert sorted(p.name for p in warm.glob("*.eml")) == ["1.eml", "2.eml"]


def test_store_errors_never_fail_persist_and_fail_hydrate_closed(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])

    def boom(*_a, **_k):
        raise OSError("down")

    monkeypatch.setattr(kv, "put", boom)
    monkeypatch.setattr(kv, "get", boom)
    monkeypatch.setattr(kv, "list_children", boom)
    assert cache_sync.persist(kv, "moq")  # reported, not raised
    with pytest.raises(GatherCacheRestoreError):
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
    _verify(new, "8")
    assert cache_sync.persist(kv, "moq") == []

    assert PREFIX + "chunk-1.tar.gz" not in kv._data  # stale chunk deleted
    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert _names(cold) == ["10.eml", "11.eml"]
    assert (cold / mbox.UIDVALIDITY_FILE).read_text() == "8"
    # The replaced epoch's mail is archived in the store, not dropped, even though
    # this host never restored it.
    assert _names(cold / "epoch-7") == ["1.eml", "2.eml", "3.eml", f"{CHUNK + 1}.eml"]


def test_unconfirmed_epoch_never_replaces_the_stored_mail(monkeypatch, tmp_path):
    # Host X still holds epoch 7; another host stored epoch 8. X's own IMAP sync
    # failed this gather, so nothing confirmed its epoch: it mustn't overwrite
    # the newer mail, including mail outside the window that exists only there.
    kv = _CountingKv()
    newer = _host(monkeypatch, tmp_path / "newer")
    _mail(newer, [10, 11], epoch="8")
    _verify(newer, "8")
    cache_sync.persist(kv, "moq")
    before = dict(kv._data)

    stale = _host(monkeypatch, tmp_path / "stale")
    _mail(stale, [1, 2], epoch="7")
    problems = cache_sync.persist(kv, "moq")
    assert kv._data == before
    assert len(problems) == 1 and "not confirmed" in problems[0]


def test_a_confirmation_does_not_outlive_its_gather(monkeypatch, tmp_path):
    # Host X confirmed epoch 7 in an earlier gather of this process; another host
    # has since stored epoch 8. X's next gather never syncs the list (discovery
    # failed, say), so nothing confirms 7 this time: the old confirmation must
    # not let persist put 7 back over 8.
    kv = _CountingKv()
    x = _host(monkeypatch, tmp_path / "x")
    _mail(x, [1, 2], epoch="7")
    _verify(x, "7")
    cache_sync.persist(kv, "moq")

    other = _host(monkeypatch, tmp_path / "other")
    _mail(other, [10, 11], epoch="8")
    _verify(other, "8")
    cache_sync.persist(kv, "moq")
    before = dict(kv._data)

    _host(monkeypatch, tmp_path / "x")  # the next gather on X, same process
    assert cache_sync.hydrate(kv, "moq") == []
    assert mbox.verified_uidvalidity(str(x)) is None
    problems = cache_sync.persist(kv, "moq")
    assert kv._data == before
    assert len(problems) == 1 and "not confirmed" in problems[0]


def test_failed_index_write_never_leaves_mismatched_mail_trusted(monkeypatch, tmp_path):
    kv = _CountingKv()
    old = _host(monkeypatch, tmp_path / "old")
    _mail(old, [1, 2, 3], epoch="7")
    cache_sync.persist(kv, "moq")

    new = _host(monkeypatch, tmp_path / "new")
    _mail(new, [1, 2], epoch="8")
    _verify(new, "8")
    index_writes = []
    real_put = kv.put

    def put(key, value, *, expect=ANY):
        if key == PREFIX + "index.json":
            index_writes.append(key)
            if len(index_writes) == 2:  # the final write, after the chunks
                raise OSError("bucket unreachable")
        return real_put(key, value, expect=expect)

    monkeypatch.setattr(kv, "put", put)
    problems = cache_sync.persist(kv, "moq")
    assert len(problems) == 1 and "not persisted" in problems[0]

    # The old index was invalidated first, so the overwritten chunk is
    # unreachable rather than trusted under the wrong epoch.
    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert list(cold.glob("*.eml")) == []  # nothing trusted as the current epoch
    assert _names(cold / "epoch-7") == ["1.eml", "2.eml", "3.eml"]  # old mail kept


def test_hydrate_keeps_a_list_cached_under_another_epoch_apart(monkeypatch, tmp_path):
    # The stored mail is real but its UIDs name other messages than the local
    # cache's: it lands in its own archive dir, and the local mail is untouched.
    kv = _CountingKv()
    other = _host(monkeypatch, tmp_path / "other")
    _mail(other, [1, 2], epoch="7")
    cache_sync.persist(kv, "moq")

    here = _host(monkeypatch, tmp_path / "here")
    _mail(here, [9], epoch="8")
    cache_sync.hydrate(kv, "moq")
    assert _names(here) == ["9.eml"]
    assert _names(here / "epoch-7") == ["1.eml", "2.eml"]


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


def test_changed_epoch_moves_the_cache_aside(tmp_path):
    _mail(tmp_path, [1, 2], epoch="7")
    _reconcile(tmp_path, b"8")
    assert list(tmp_path.glob("*.eml")) == []
    assert _names(tmp_path / "epoch-7") == ["1.eml", "2.eml"]
    assert (tmp_path / "epoch-7" / "1.eml").read_text().startswith("Message-Id: <1@x>")
    assert (tmp_path / mbox.UIDVALIDITY_FILE).read_text() == "8"

    _mail(tmp_path, [5], epoch="8")  # the refetch under the new numbering
    _reconcile(tmp_path, b"9")  # and it changes again: both archives survive
    assert _names(tmp_path / "epoch-7") == ["1.eml", "2.eml"]
    assert _names(tmp_path / "epoch-8") == ["5.eml"]


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


# --- failures are reported, not swallowed -----------------------------------


def test_failed_chunk_fetch_fails_the_restore(monkeypatch, tmp_path):
    # Publishing without that chunk's mail would delete the threads built from
    # it, so a store error on a chunk is fatal -- after everything else is
    # restored, so the failure is as small as it can be.
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2, CHUNK + 1])
    cache_sync.persist(kv, "moq")

    cold = _host(monkeypatch, tmp_path / "cold")
    real_get = kv.get

    def get(key):
        if key.endswith("chunk-0.tar.gz"):
            raise OSError("bucket unreachable")
        return real_get(key)

    monkeypatch.setattr(kv, "get", get)
    with pytest.raises(GatherCacheRestoreError) as info:
        cache_sync.hydrate(kv, "moq")
    assert "'moq'" in str(info.value) and "1 stored chunk(s) could not be fetched" in str(info.value)
    assert [p.name for p in cold.glob("*.eml")] == [f"{CHUNK + 1}.eml"]  # the rest came


def test_failed_listing_fails_the_restore(monkeypatch, tmp_path):
    kv = _CountingKv()
    _host(monkeypatch, tmp_path / "cold")

    def boom(_prefix):
        raise OSError("down")

    monkeypatch.setattr(kv, "list_children", boom)
    with pytest.raises(GatherCacheRestoreError, match="listing failed"):
        cache_sync.hydrate(kv, "moq")


def test_failed_index_fetch_fails_the_restore(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])
    cache_sync.persist(kv, "moq")

    _host(monkeypatch, tmp_path / "cold")
    real_get = kv.get

    def get(key):
        if key.endswith("index.json"):
            raise OSError("bucket unreachable")
        return real_get(key)

    monkeypatch.setattr(kv, "get", get)
    with pytest.raises(GatherCacheRestoreError, match="'moq' not restored"):
        cache_sync.hydrate(kv, "moq")


def test_a_missing_chunk_is_a_note_not_a_failure(monkeypatch, tmp_path):
    # A retry can't bring back an object that isn't there; failing would wedge
    # the corpus. Say so, and let persist rebuild the chunk from this host.
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2, CHUNK + 1])
    cache_sync.persist(kv, "moq")
    kv.delete(PREFIX + "chunk-0.tar.gz")

    cold = _host(monkeypatch, tmp_path / "cold")
    problems = cache_sync.hydrate(kv, "moq")
    assert len(problems) == 1 and "missing or unreadable" in problems[0]
    assert [p.name for p in cold.glob("*.eml")] == [f"{CHUNK + 1}.eml"]


def test_a_corrupt_chunk_is_a_note_not_a_failure(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])
    cache_sync.persist(kv, "moq")
    kv.put(PREFIX + "chunk-0.tar.gz", b"not a gzip stream")

    cold = _host(monkeypatch, tmp_path / "cold")
    problems = cache_sync.hydrate(kv, "moq")
    assert len(problems) == 1 and "missing or unreadable" in problems[0]
    assert list(cold.glob("*.eml")) == []


def test_failed_persist_is_reported(monkeypatch, tmp_path):
    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _mail(warm, [1, 2])

    def boom(*_a, **_k):
        raise OSError("down")

    monkeypatch.setattr(kv, "put", boom)
    problems = cache_sync.persist(kv, "moq")
    assert any("'moq' not persisted" in p for p in problems)


# --- the property the change exists for -------------------------------------


def _write_eml(list_dir: Path, uid: int, mid: str, subject: str, reply_to: str = "") -> None:
    headers = [
        f"Message-Id: <{mid}>",
        f"Subject: {subject}",
        "From: Alice <alice@example.com>",
        f"Date: Mon, 0{1 + uid % 9} Jan 2024 10:00:00 +0000",
    ]
    if reply_to:
        headers.append(f"In-Reply-To: <{reply_to}>")
    (list_dir / f"{uid}.eml").write_text("\n".join(headers) + f"\n\nbody {uid}\n")


def test_cold_host_threads_match_the_warm_hosts(monkeypatch, tmp_path):
    # Writer -> reader round trip: threads rebuilt on a fresh host from the
    # restored mail must equal the warm host's, or the next publish would drop
    # the old ones (write_thread_files deletes any thread it doesn't regenerate).
    from ietf_llm.gather.sources import mail_threads

    def shape() -> List[Tuple[str, List[str]]]:
        threads = mail_threads.build_threads("moq")
        return sorted((t.root.message_id, [m.message_id for m in t.members]) for t in threads)

    kv = _CountingKv()
    warm = _host(monkeypatch, tmp_path / "warm")
    _write_eml(warm, 1, "a@x", "Topic one")
    _write_eml(warm, 2, "b@x", "Re: Topic one", reply_to="a@x")
    _write_eml(warm, CHUNK + 5, "c@x", "Another topic")
    (warm / mbox.UIDVALIDITY_FILE).write_text("7")
    expected = shape()
    assert len(expected) == 2
    cache_sync.persist(kv, "moq")

    shutil.rmtree(tmp_path / "warm")
    cold = _host(monkeypatch, tmp_path / "cold")
    assert cache_sync.hydrate(kv, "moq") == []
    assert sorted(p.name for p in cold.glob("*.eml")) == ["1.eml", "2.eml", f"{CHUNK + 5}.eml"]
    assert shape() == expected


# --- the sync wires the epoch check in correctly ----------------------------


class _FakeImap:
    """Just enough IMAP4_SSL for one `_imap_sync_attempt`."""

    def __init__(self, epoch: bytes, uids: List[int]) -> None:
        self._epoch = epoch
        self._uids = uids

    def login(self, *_a):
        return ("OK", [b""])

    def select(self, *_a, **_k):
        return ("OK", [b"2"])

    def response(self, name: str):
        assert name == "UIDVALIDITY"
        return (name, [self._epoch])

    def uid(self, command: str, *args):
        if command == "search":
            return ("OK", [b" ".join(str(u).encode() for u in self._uids)])
        wanted = [int(u) for u in args[0].split(",")]
        return (
            "OK",
            [(f"{u} (UID {u} RFC822 {{5}}".encode(), f"new {u}".encode()) for u in wanted],
        )

    def logout(self):
        return ("BYE", [b""])


def _attempt(monkeypatch, list_dir: Path, fake: _FakeImap):
    monkeypatch.setattr(mbox.imaplib, "IMAP4_SSL", lambda *_a, **_k: fake)
    return mbox._imap_sync_attempt("moq", None, str(list_dir), Verbosity.QUIET, None)


def test_epoch_change_clears_the_cache_before_the_cached_set_is_read(monkeypatch, tmp_path):
    list_dir = _host(monkeypatch, tmp_path / "h")
    (list_dir / "1.eml").write_text("stale: another message under the old numbering")
    (list_dir / mbox.UIDVALIDITY_FILE).write_text("7")

    uids, new, _ = _attempt(monkeypatch, list_dir, _FakeImap(b"8", [1, 2]))
    assert (uids, new) == (["1", "2"], 2)  # UID 1 refetched, not skipped as cached
    assert (list_dir / "1.eml").read_text() == "new 1"
    assert (list_dir / mbox.UIDVALIDITY_FILE).read_text() == "8"
    assert mbox.verified_uidvalidity(str(list_dir)) == "8"
    # A file, so the persist that runs in the parent of the gather child sees it.
    assert (list_dir / mbox.VERIFIED_FILE).read_text() == "8"


def test_unchanged_epoch_skips_cached_messages(monkeypatch, tmp_path):
    list_dir = _host(monkeypatch, tmp_path / "h")
    (list_dir / "1.eml").write_text("cached")
    (list_dir / mbox.UIDVALIDITY_FILE).write_text("7")

    uids, new, _ = _attempt(monkeypatch, list_dir, _FakeImap(b"7", [1, 2]))
    assert (uids, new) == (["1", "2"], 1)
    assert (list_dir / "1.eml").read_text() == "cached"


def test_a_sync_that_fails_leaves_the_epoch_unverified(monkeypatch, tmp_path):
    list_dir = _host(monkeypatch, tmp_path / "h")
    mbox.record_verified(str(list_dir), "7")  # left over from an earlier gather

    def boom(*_a):
        raise OSError("timed out")

    monkeypatch.setattr(mbox, "_imap_sync_attempt", boom)
    mbox._sync_one_list("moq", "moq", 12, Verbosity.QUIET)
    assert mbox.verified_uidvalidity(str(list_dir)) is None


# --- markers and responses that aren't clean ---------------------------------


@pytest.mark.parametrize("marker", [b"", b"abc", b"\xff\xfe", b"7 8", b"-3"])
def test_a_corrupt_marker_is_adopted_over_not_read_as_a_change(tmp_path, marker):
    (tmp_path / "1.eml").write_text("keep")
    (tmp_path / mbox.UIDVALIDITY_FILE).write_bytes(marker)
    assert mbox.read_uidvalidity(str(tmp_path)) is None

    _reconcile(tmp_path, b"7")
    assert (tmp_path / "1.eml").exists()
    assert (tmp_path / mbox.UIDVALIDITY_FILE).read_text() == "7"


def test_a_corrupt_marker_is_unknown_to_the_cloud_sync_too(monkeypatch, tmp_path):
    kv = _CountingKv()
    other = _host(monkeypatch, tmp_path / "other")
    _mail(other, [1], epoch="7")
    cache_sync.persist(kv, "moq")

    here = _host(monkeypatch, tmp_path / "here")
    (here / mbox.UIDVALIDITY_FILE).write_bytes(b"\xff\xfe")
    cache_sync.hydrate(kv, "moq")
    assert (here / "1.eml").exists()  # restored, as for an unmarked cache


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"3857529045", "3857529045"),
        (b"123] UIDs [valid", "123"),  # imaplib's greedy response-code regex
        (b"  42", "42"),
        (b"12abc", None),
        (b"garbage", None),
        (None, None),
    ],
)
def test_server_uidvalidity_parsing(raw, expected):
    assert mbox._server_uidvalidity(_Mail(raw)) == expected  # type: ignore[arg-type]


# --- epoch change keeps the old mail ------------------------------------------


def test_epoch_change_through_a_cold_host_keeps_every_message(monkeypatch, tmp_path):
    # The whole path: a cold host restores epoch 7, its sync finds the server at
    # 8 (moving the mail aside) and refetches the window, and a later fresh host
    # still has the old mail -- including what only ever lived in the cache.
    from ietf_llm.gather.sources import mail_threads

    def shape() -> List[Tuple[str, List[str]]]:
        threads = mail_threads.build_threads("moq")
        return sorted((t.root.message_id, [m.message_id for m in t.members]) for t in threads)

    kv = _CountingKv()
    first = _host(monkeypatch, tmp_path / "first")
    _write_eml(first, 1, "old-a@x", "Old topic")
    _write_eml(first, 2, "old-b@x", "Re: Old topic", reply_to="old-a@x")
    (first / mbox.UIDVALIDITY_FILE).write_text("7")
    cache_sync.persist(kv, "moq")

    second = _host(monkeypatch, tmp_path / "second")
    assert cache_sync.hydrate(kv, "moq") == []
    _reconcile(second, b"8")  # the sync: server renumbered
    _write_eml(second, 1, "new-a@x", "New topic")  # window refetched as UID 1
    assert cache_sync.persist(kv, "moq") == []
    expected = shape()
    assert [root for root, _ in expected] == ["<new-a@x>", "<old-a@x>"]
    assert ("<old-a@x>", ["<old-a@x>", "<old-b@x>"]) in expected  # the old thread survived

    third = _host(monkeypatch, tmp_path / "third")
    assert cache_sync.hydrate(kv, "moq") == []
    assert (third / mbox.UIDVALIDITY_FILE).read_text() == "8"
    assert shape() == expected
    assert PREFIX + "epoch-7/index.json" in kv._data


def test_a_stale_host_does_not_store_the_current_epoch_twice(monkeypatch, tmp_path):
    # Local epoch 7, stored epoch 8, and this gather's sync lands on 8: hydrate
    # put the stored mail in epoch-8/, but that is the main unit's mail already.
    kv = _CountingKv()
    newer = _host(monkeypatch, tmp_path / "newer")
    _mail(newer, [10, 11], epoch="8")
    _verify(newer, "8")
    cache_sync.persist(kv, "moq")

    stale = _host(monkeypatch, tmp_path / "stale")
    _mail(stale, [1, 2], epoch="7")
    cache_sync.hydrate(kv, "moq")
    assert _names(stale / "epoch-8") == ["10.eml", "11.eml"]
    _reconcile(stale, b"8")  # the sync: server is at 8
    _mail(stale, [12], epoch="8")
    assert cache_sync.persist(kv, "moq") == []

    assert PREFIX + "epoch-8/index.json" not in kv._data  # not stored twice
    assert PREFIX + "epoch-7/index.json" in kv._data  # the stale host's own mail kept
    cold = _host(monkeypatch, tmp_path / "cold")
    cache_sync.hydrate(kv, "moq")
    assert _names(cold) == ["10.eml", "11.eml", "12.eml"]  # folded, none lost
    assert _names(cold / "epoch-7") == ["1.eml", "2.eml"]


def test_epoch_change_is_abandoned_if_the_old_mail_cannot_be_archived(monkeypatch, tmp_path):
    kv = _CountingKv()
    old = _host(monkeypatch, tmp_path / "old")
    _mail(old, [1, 2, 3], epoch="7")
    cache_sync.persist(kv, "moq")
    before = dict(kv._data)

    new = _host(monkeypatch, tmp_path / "new")
    _mail(new, [1], epoch="8")
    _verify(new, "8")
    real_get = kv.get

    def get(key):
        if key.endswith("chunk-0.tar.gz"):
            raise OSError("bucket unreachable")
        return real_get(key)

    monkeypatch.setattr(kv, "get", get)
    problems = cache_sync.persist(kv, "moq")
    assert len(problems) == 1 and "could not be fetched to archive" in problems[0]
    assert kv._data == before  # epoch 7 still stored, untouched


def test_a_failed_archive_fetch_fails_the_restore(monkeypatch, tmp_path):
    kv = _CountingKv()
    old = _host(monkeypatch, tmp_path / "old")
    _mail(old, [1, 2], epoch="7")
    cache_sync.persist(kv, "moq")
    new = _host(monkeypatch, tmp_path / "new")
    _mail(new, [5], epoch="8")
    _verify(new, "8")
    cache_sync.persist(kv, "moq")  # epoch-7 is now an archive unit

    _host(monkeypatch, tmp_path / "cold")
    real_get = kv.get

    def get(key):
        if "epoch-7/chunk-" in key:
            raise OSError("bucket unreachable")
        return real_get(key)

    monkeypatch.setattr(kv, "get", get)
    with pytest.raises(GatherCacheRestoreError, match="1 stored chunk"):
        cache_sync.hydrate(kv, "moq")


def test_forget_verified_drops_every_lists_confirmation(monkeypatch, tmp_path):
    first = _host(monkeypatch, tmp_path / "h")
    second = first.parent / "moq-chairs"
    second.mkdir()
    other_corpus = tmp_path / "h" / "imap-cache" / "tls" / "tls"
    other_corpus.mkdir(parents=True)
    for directory in (first, second, other_corpus):
        mbox.record_verified(str(directory), "7")

    mbox.forget_verified("moq")

    assert mbox.verified_uidvalidity(str(first)) is None
    assert mbox.verified_uidvalidity(str(second)) is None
    assert mbox.verified_uidvalidity(str(other_corpus)) == "7"


def test_a_stray_file_beside_the_lists_does_not_abort_hydrate(monkeypatch, tmp_path):
    kv = _CountingKv()
    list_dir = _host(monkeypatch, tmp_path / "h")
    (list_dir.parent / "stray.txt").write_text("not a list dir")
    mbox.record_verified(str(list_dir), "7")

    mbox.forget_verified("moq")  # must not raise NotADirectoryError
    assert mbox.verified_uidvalidity(str(list_dir)) is None

    # and hydrate gets past it to the rest of its work
    datatracker_key = "corpora/moq/gather-cache/http-cache.json"
    kv.put(datatracker_key, b'{"u": {"etag": "e", "body": "{}", "last_used": 1}}')
    assert cache_sync.hydrate(kv, "moq") == []
    assert os.path.exists(cache_sync.datatracker.http_cache_path("moq"))


def test_an_unwritable_confirmation_is_logged(monkeypatch, tmp_path, capsys):
    list_dir = _host(monkeypatch, tmp_path / "h")
    monkeypatch.setattr(
        mbox, "write_if_changed", lambda *a: (_ for _ in ()).throw(OSError("disk full"))
    )
    mbox.record_verified(str(list_dir), "7")
    assert mbox.verified_uidvalidity(str(list_dir)) is None
    assert "disk full" in capsys.readouterr().err


# --- a shortfall does not stop the epoch from being confirmed ----------------


class _BadUidImap(_FakeImap):
    """A server whose UIDs in `bad` are never delivered (any FETCH naming one is
    refused); the rest come back as parseable messages."""

    def __init__(self, epoch: bytes, uids: List[int], bad: set[int]) -> None:
        super().__init__(epoch, uids)
        self._bad = bad

    def uid(self, command: str, *args):
        if command == "search":
            return super().uid(command, *args)
        wanted = [int(u) for u in args[0].split(",")]
        if self._bad & set(wanted):
            return ("NO", [b"corrupt"])
        return (
            "OK",
            [
                (
                    f"{u} (UID {u} RFC822 {{5}}".encode(),
                    f"Message-Id: <new-{u}@x>\nSubject: New {u}\n"
                    f"From: Bob <bob@example.com>\nDate: Mon, 01 Feb 2024 10:00:00 +0000\n\nbody".encode(),
                )
                for u in wanted
            ],
        )


def _sync_with(monkeypatch, fake: _FakeImap) -> List[str]:
    monkeypatch.setattr(mbox.imaplib, "IMAP4_SSL", lambda *_a, **_k: fake)
    notes: List[str] = []
    mbox._sync_one_list("moq", "moq", 12, Verbosity.QUIET, note_fn=notes.append)
    return notes


def _message_ids(wg: str = "moq") -> List[str]:
    from ietf_llm.gather.sources import mail_threads

    return sorted(m.message_id for t in mail_threads.build_threads(wg) for m in t.members)


def test_a_renumbered_epoch_with_an_undeliverable_uid_still_replaces_and_archives(
    monkeypatch, tmp_path
):
    kv = _CountingKv()
    first = _host(monkeypatch, tmp_path / "first")
    _write_eml(first, 1, "old-a@x", "Old topic")
    _write_eml(first, 2, "old-b@x", "Re: Old topic", reply_to="old-a@x")
    (first / mbox.UIDVALIDITY_FILE).write_text("7")
    cache_sync.persist(kv, "moq")
    before = _message_ids()

    # The server now reports epoch 8, lists UIDs 1..3 and will not deliver 2.
    second = _host(monkeypatch, tmp_path / "second")
    assert cache_sync.hydrate(kv, "moq") == []
    notes = _sync_with(monkeypatch, _BadUidImap(b"8", [1, 2, 3], bad={2}))
    assert any("undeliverable UIDs: 2" in n for n in notes)
    assert mbox.verified_uidvalidity(str(second)) == "8"  # still confirmed

    assert cache_sync.persist(kv, "moq") == []
    assert PREFIX + "epoch-7/index.json" in kv._data  # E1 archived
    cold = _host(monkeypatch, tmp_path / "cold")
    assert cache_sync.hydrate(kv, "moq") == []
    assert (cold / mbox.UIDVALIDITY_FILE).read_text() == "8"
    assert _names(cold) == ["1.eml", "3.eml"]  # the main unit is E2, minus UID 2
    after = _message_ids()
    assert set(before) <= set(after)  # every old Message-Id survives, via epoch-7/
    assert {"<new-1@x>", "<new-3@x>"} <= set(after)


def test_a_same_epoch_shortfall_persists_additively(monkeypatch, tmp_path):
    kv = _CountingKv()
    first = _host(monkeypatch, tmp_path / "first")
    _mail(first, [1, 2, 3], epoch="7")
    cache_sync.persist(kv, "moq")

    # A host that lacks UID 2 locally and whose server will not deliver it.
    second = _host(monkeypatch, tmp_path / "second")
    _mail(second, [1, 3], epoch="7")
    _sync_with(monkeypatch, _BadUidImap(b"7", [1, 2, 3], bad={2}))
    assert mbox.verified_uidvalidity(str(second)) == "7"
    assert cache_sync.persist(kv, "moq") == []

    # The stored chunk still holds all three; it was not replaced by less.
    cold = _host(monkeypatch, tmp_path / "cold")
    assert cache_sync.hydrate(kv, "moq") == []
    assert _names(cold) == ["1.eml", "2.eml", "3.eml"]
