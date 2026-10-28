"""Round-trip the gather accelerator caches to the cloud KvStore, so an
ephemeral host (scale-to-zero) doesn't rebuild them by re-hitting rate-limited
upstreams after its scratch is wiped (issue #82).

Only the cloud CorpusStore backend calls in; the local backend keeps these
caches on persistent disk and needs no sync. The caches have different
contention profiles, so each is scoped to the unit that already guarantees a
single writer:

  - `.http-cache.json` — the datatracker ETag store. Sharded **per corpus** under
    that corpus's prefix. #85's per-corpus gather lease serialises writers across
    the fleet, and `bind_gather_corpus` binds a per-corpus ETag store to *this
    thread* so the runner's up-to-N concurrent in-process gathers each write their
    own corpus file (not one shared root file). Together that makes hydrate ->
    gather -> flush -> persist a plain read-modify-write — no CAS, no lost delta.
  - `_github-users.json` / `_datatracker-github.json` / `_datatracker-people.json`
    — corpus-independent identity maps, **shared** at a fleet prefix. Two
    different-corpus gathers can run at once, so persist does a bounded
    compare-and-swap merge: lossless, and cheap because contention is rare and
    the maps are append-mostly.
  - `_catalog/` — the `find_efforts` effort catalog (the Datatracker group
    collection — the same rate-limited upstream as the ETag store). A
    **shared fleet singleton** mirrored once per gather; every gather writes
    identical content, so last-writer-wins is correct — no CAS, no merge. A
    directory of a few files (raw source slices + the derived `catalog.json`,
    each with its `.etag` sidecar), so it round-trips as a key per file under one
    fleet prefix; the restored sidecars let the next gather revalidate (304)
    rather than re-fetch. (`_rfc/` — the same shape but a non-rate-limited CDN
    mirror — is deferred; see issue #82.)
  - `imap-cache/<corpus>/<list>/` — the raw `.eml` mail. Thread reconstruction
    re-reads *every* cached message and deletes thread files it doesn't
    regenerate, so a fresh host needs the whole archive back, not just what is
    new. Sharded **per corpus** (lease-serialised, so no CAS) into gzip-tar
    chunks of `_IMAP_CHUNK_UIDS` consecutive UIDs, so a restore is tens of GETs
    rather than one per message, plus a per-list `index.json` naming each
    chunk's UIDs and the UIDVALIDITY epoch. A chunk's UIDs are not frozen (a
    wider `--months` window fills in older ones), so persist re-uploads any
    chunk whose UID set changed, and never overwrites a stored chunk with fewer
    messages than it holds: mail that has aged out of the window survives only
    in the cache. A differing UIDVALIDITY epoch replaces the stored mail only
    when this gather's sync confirmed the local epoch with the server, and the
    replaced epoch's mail is kept as its own archive unit (`<list>/epoch-<n>/`,
    the same index-plus-chunks shape) rather than dropped: it is real mail, and
    the only copy of anything older than the window.

Best-effort throughout, with one exception: a sync failure doesn't fail a gather
(it just means a cold rebuild next time), matching how
`_save_cache`/`_HttpCache.flush` already swallow their own write errors, and each
cache syncs independently, so one cache's failure never skips the others. The
exception is a stored IMAP chunk that can't be fetched. A rebuild from what's
left would publish a version without the threads whose mail didn't come back
(the thread writer deletes any thread file it doesn't regenerate), so `hydrate`
raises `GatherCacheRestoreError` and the gather fails; the next one retries.
Failures a retry can't fix (a chunk or index that is missing or corrupt) are
notes, since failing would wedge the corpus: persist rebuilds them from local.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import tarfile
import zlib
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from ...paths import get_cache_dir
from ...singletons.catalog import catalog_index_dir
from ...store.corpus import GatherCacheRestoreError
from ...store.kv import ABSENT, KvStore
from . import (
    datatracker,
    datatracker_github,
    datatracker_people,
    github_users,
    mbox,
)

#: Per-corpus key — lease-serialised single writer, so a plain RMW is safe.
_HTTP_CACHE_KEY = "corpora/{corpus}/gather-cache/http-cache.json"
#: Per-corpus prefix for the IMAP mail cache: `<list>/index.json` plus
#: `<list>/chunk-<n>.tar.gz`. Same single-writer (lease) reasoning as above.
_IMAP_PREFIX = "corpora/{corpus}/gather-cache/imap/"
_IMAP_INDEX = "index.json"
#: Consecutive UIDs per chunk. Messages run ~10-30 KB, so a chunk is a few MB
#: compressed: big enough that a large list is tens of objects, small enough
#: that the tail chunk a gather touches is cheap to re-upload.
_IMAP_CHUNK_UIDS = 512
_EML_NAME = re.compile(r"^(\d+)\.eml$")
_EPOCH_DIR = re.compile(rf"^{re.escape(mbox.EPOCH_DIR_PREFIX)}(\d+)$")
#: Shared fleet keys — concurrent writers, so persist uses a CAS-merge.
_GITHUB_USERS_KEY = "fleet/gather-cache/github-users.json"
_DATATRACKER_GITHUB_KEY = "fleet/gather-cache/datatracker-github.json"
_DATATRACKER_PEOPLE_KEY = "fleet/gather-cache/datatracker-people.json"
#: Shared fleet prefix for the effort-catalog directory — one key per file,
#: last-writer-wins (every gather mirrors identical upstream content).
_CATALOG_PREFIX = "fleet/catalog/"

#: Bounded retries for the shared-map compare-and-swap (mirrors `store.control`).
_CAS_RETRIES = 8

MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


def hydrate(kv: KvStore, corpus: str) -> List[str]:
    """Pull the persisted caches into the local cache dir before a gather, so the
    gather revalidates (304s) and reuses identity lookups instead of re-fetching.
    Returns notes for failures worth surfacing. Raises `GatherCacheRestoreError`,
    after restoring everything else, if stored IMAP mail couldn't be fetched (see
    `_restore_imap`); nothing else here raises."""
    # A UIDVALIDITY confirmation is good for one gather, from here to persist.
    # One left over from an earlier gather in this process would let persist
    # replace the stored mail on the strength of a check this gather never made.
    mbox.forget_verified(corpus)
    _restore(
        kv, _HTTP_CACHE_KEY.format(corpus=corpus), datatracker.http_cache_path(corpus)
    )
    # Bind this thread's ETag store to the freshly-hydrated per-corpus file. The
    # runner gathers up to N corpora concurrently, one per worker thread, so a
    # per-thread per-corpus store is what keeps them off one shared file.
    try:
        datatracker.bind_gather_corpus(corpus)
    except Exception:  # pylint: disable=broad-except
        pass
    _restore(kv, _GITHUB_USERS_KEY, github_users.cache_path())
    _restore(kv, _DATATRACKER_GITHUB_KEY, datatracker_github.cache_path())
    _restore(kv, _DATATRACKER_PEOPLE_KEY, datatracker_people.cache_path())
    _restore_tree(kv, _CATALOG_PREFIX, catalog_index_dir())
    problems: List[str] = []
    fatal = _restore_imap(kv, corpus, problems)
    if fatal:
        raise GatherCacheRestoreError("; ".join(fatal + problems))
    return problems


def persist(kv: KvStore, corpus: str) -> List[str]:
    """Push the caches back to durable storage after a gather. Never raises;
    returns notes for failures worth surfacing (see `_persist_imap`)."""
    # The ETag store flushes only at interpreter exit, which never fires between
    # gathers in the serve worker — force it so the file reflects this gather.
    try:
        datatracker.flush_http_cache()
    except Exception:  # pylint: disable=broad-except
        pass
    _put_plain(
        kv, _HTTP_CACHE_KEY.format(corpus=corpus), datatracker.http_cache_path(corpus)
    )
    # Unbind this thread's per-corpus store (release it; the next gather on this
    # reused worker binds its own).
    try:
        datatracker.bind_gather_corpus(None)
    except Exception:  # pylint: disable=broad-except
        pass
    _cas_merge(
        kv, _GITHUB_USERS_KEY, github_users.cache_path(), github_users.merge_cache
    )
    _cas_merge(
        kv,
        _DATATRACKER_GITHUB_KEY,
        datatracker_github.cache_path(),
        datatracker_github.merge_cache,
    )
    _cas_merge(
        kv,
        _DATATRACKER_PEOPLE_KEY,
        datatracker_people.cache_path(),
        datatracker_people.merge_cache,
    )
    _persist_tree(kv, _CATALOG_PREFIX, catalog_index_dir())
    problems: List[str] = []
    _persist_imap(kv, corpus, problems)
    return problems


def clear(kv: KvStore, corpus: str) -> None:
    """Delete this corpus's stored gather caches (`--clear-cache`): the ETag store
    and all the IMAP mail, archives included. Unlike hydrate and persist this
    raises on a store error, since the caller asked for the state to be gone and
    a half-done clear would let the next hydrate bring it back. Per-corpus keys
    only: the shared identity maps and the catalog are other corpora's too."""
    kv.delete(_HTTP_CACHE_KEY.format(corpus=corpus))
    prefix = _IMAP_PREFIX.format(corpus=corpus)
    for name in kv.list_children(prefix):
        _clear_imap_unit(kv, f"{prefix}{name}/")


def _clear_imap_unit(kv: KvStore, prefix: str) -> None:
    """Delete a list's stored units, the index before its chunks so a failure
    part way leaves an index naming fewer chunks, never a chunk-less index claim."""
    children = kv.list_children(prefix)
    for name in sorted(children, key=lambda child: child != _IMAP_INDEX):
        if _EPOCH_DIR.match(name):
            _clear_imap_unit(kv, f"{prefix}{name}/")
        else:
            kv.delete(prefix + name)


# --- per-cache operations -------------------------------------------------


def _restore(kv: KvStore, key: str, path: str) -> None:
    """Write the blob stored at `key` to local `path` (atomic temp+rename). A
    miss (nothing stored yet) or any error leaves the local file untouched."""
    try:
        record = kv.get(key)
        if record is None:
            return
        _atomic_write(path, record[0])
    except Exception:  # pylint: disable=broad-except
        pass


def _put_plain(kv: KvStore, key: str, path: str) -> None:
    """Upload local `path` unconditionally. CAS-free because the caller holds the
    per-corpus gather lease, so it is the only writer of this key."""
    try:
        data = _read(path)
        if data is None:
            return
        kv.put(key, data)
    except Exception:  # pylint: disable=broad-except
        pass


def _cas_merge(kv: KvStore, key: str, path: str, merge: MergeFn) -> None:
    """Merge local `path` into the stored blob under compare-and-swap, retrying a
    bounded number of times. Lossless: a writer that loses the race re-reads,
    re-merges, and retries rather than clobbering a concurrent gather's delta."""
    try:
        local = _read_json(path)
        if local is None:
            return
        for _ in range(_CAS_RETRIES):
            record = kv.get(key)
            if record is None:
                remote: Dict[str, Any] = {}
                expect: object = ABSENT
            else:
                parsed = json.loads(record[0])
                remote = parsed if isinstance(parsed, dict) else {}
                expect = record[1]
            payload = json.dumps(merge(remote, local), sort_keys=True).encode("utf-8")
            if kv.put(key, payload, expect=expect) is not None:
                return
    except Exception:  # pylint: disable=broad-except
        pass


# --- directory tree (fleet singleton, last-writer-wins) -------------------


def _restore_tree(kv: KvStore, prefix: str, local_dir: str) -> None:
    """Restore every file stored under `prefix` into `local_dir` (one key per
    file). Enumerated from the store, so the filename set isn't hardcoded; the
    restored `.etag` sidecars let the next gather revalidate rather than
    re-fetch. Best-effort per file — one failure never skips the rest."""
    try:
        names = kv.list_children(prefix)
    except Exception:  # pylint: disable=broad-except
        return
    for name in names:
        _restore(kv, prefix + name, os.path.join(local_dir, name))


def _persist_tree(kv: KvStore, prefix: str, local_dir: str) -> None:
    """Upload every regular file in `local_dir` to `prefix` unconditionally
    (last-writer-wins: every gather mirrors identical upstream content, so no CAS
    is needed). Skips atomic-write `.tmp` leftovers.

    Additive only — it never deletes store keys. A file dropped from the local
    tree upstream therefore lingers under `prefix` and keeps being re-hydrated.
    Acceptable because the catalog's filename set is stable (the source slices and
    the derived `catalog.json`); revisit with a prune step if that set churns."""
    try:
        names = os.listdir(local_dir)
    except OSError:
        return
    for name in names:
        if name.endswith(".tmp"):
            continue
        path = os.path.join(local_dir, name)
        if os.path.isfile(path):
            _put_plain(kv, prefix + name, path)


# --- IMAP mail cache (per corpus, chunked) --------------------------------


class _ImapIndex(NamedTuple):
    """What a list's stored chunks hold: the UIDVALIDITY epoch they were fetched
    under (None if unknown) and the UIDs in each chunk."""

    uidvalidity: Optional[str]
    chunks: Dict[int, Set[int]]


def _imap_root(corpus: str) -> str:
    return os.path.join(get_cache_dir(), "imap-cache", corpus)


def _chunk_key(prefix: str, chunk: int) -> str:
    return f"{prefix}chunk-{chunk}.tar.gz"


class _CorruptIndex(ValueError):
    """The stored index isn't readable as one."""


def _load_imap_index(kv: KvStore, key: str) -> _ImapIndex:
    """The stored index, or an empty one if none is stored yet. Raises
    `_CorruptIndex` on a corrupt one (callers skip the list rather than treat the
    store as empty and overwrite chunks they can no longer account for), and
    whatever the store raises if it can't be read."""
    record = kv.get(key)
    if record is None:
        return _ImapIndex(None, {})
    try:
        parsed = json.loads(record[0])
        chunks = {
            int(c): {int(u) for u in uids} for c, uids in parsed["chunks"].items()
        }
        epoch = parsed.get("uidvalidity")
    except (ValueError, KeyError, TypeError, AttributeError) as err:
        raise _CorruptIndex(f"unreadable index {key}") from err
    return _ImapIndex(str(epoch) if epoch else None, chunks)


def _dump_imap_index(index: _ImapIndex) -> bytes:
    payload = {
        "uidvalidity": index.uidvalidity,
        "chunks": {str(c): sorted(uids) for c, uids in sorted(index.chunks.items())},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _local_chunks(list_dir: str) -> Dict[int, Set[int]]:
    """The cached UIDs under `list_dir`, grouped by chunk."""
    chunks: Dict[int, Set[int]] = {}
    try:
        names = os.listdir(list_dir)
    except OSError:
        return chunks
    for name in names:
        match = _EML_NAME.match(name)
        if match:
            uid = int(match.group(1))
            chunks.setdefault(uid // _IMAP_CHUNK_UIDS, set()).add(uid)
    return chunks


def _epochs_differ(local: Optional[str], stored: Optional[str]) -> bool:
    """Both epochs known and unequal. An unknown side matches anything: the
    sync adopts a cache that has no marker, so there is nothing to contradict."""
    return local is not None and stored is not None and local != stored


def _pack_chunk(members: Dict[int, bytes]) -> bytes:
    """Deterministic gzip tar of `<uid>.eml` members (no timestamps), so the same
    mail always yields the same bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for uid, data in sorted(members.items()):
                info = tarfile.TarInfo(f"{uid}.eml")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack_chunk(blob: bytes) -> Dict[int, bytes]:
    members: Dict[int, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar:
            match = _EML_NAME.match(member.name)
            handle = tar.extractfile(member) if match and member.isreg() else None
            if match and handle is not None:
                members[int(match.group(1))] = handle.read()
    return members


def _restore_imap(kv: KvStore, corpus: str, problems: List[str]) -> List[str]:
    """Restore every stored list's mail into the local IMAP cache. A failure on
    one list or chunk never skips the rest. Returns what couldn't be fetched
    because the store errored (a retry may succeed); `hydrate` fails the gather
    on those. What a retry can't fix -- a chunk or index that is missing or
    corrupt -- goes in `problems` as a note. Either way mail older than the
    search window would be absent when threads are rebuilt, and the thread
    writer deletes any thread file it doesn't regenerate."""
    fatal: List[str] = []
    prefix = _IMAP_PREFIX.format(corpus=corpus)
    try:
        names = kv.list_children(prefix)
    except Exception as err:  # pylint: disable=broad-except
        return [f"IMAP mail cache not restored (listing failed: {err})"]
    root = _imap_root(corpus)
    for name in names:
        if os.path.basename(name) != name or name.startswith("."):
            continue
        try:
            failed, lost = _restore_imap_list(
                kv, f"{prefix}{name}/", os.path.join(root, name)
            )
        except _CorruptIndex as err:
            problems.append(f"IMAP mail cache for '{name}' not restored ({err})")
            continue
        except Exception as err:  # pylint: disable=broad-except
            fatal.append(f"IMAP mail cache for '{name}' not restored ({err})")
            continue
        if failed:
            fatal.append(
                f"IMAP mail cache for '{name}': {failed} stored chunk(s) could "
                "not be fetched"
            )
        if lost:
            problems.append(
                f"IMAP mail cache for '{name}': {lost} stored chunk(s) missing "
                "or unreadable; the next persist rebuilds them from this host"
            )
    return fatal


def _restore_imap_list(kv: KvStore, prefix: str, list_dir: str) -> Tuple[int, int]:
    """Restore one list; returns `(fetch failures, chunks missing or corrupt)`.

    A list is a main unit (the current epoch's mail, in `list_dir`) plus one
    archive unit per earlier epoch (`epoch-<n>/`, see `mbox.EPOCH_DIR_PREFIX`),
    each stored as its own index and chunks. Mail from a different epoch than
    the local cache's lands in its own archive dir rather than being skipped or
    mixed in: its UIDs name other messages, but the mail is real and thread
    reconstruction walks the list dir recursively."""
    index = _load_imap_index(kv, prefix + _IMAP_INDEX)
    local_epoch = mbox.read_uidvalidity(list_dir)
    if _epochs_differ(local_epoch, index.uidvalidity):
        target = os.path.join(list_dir, f"{mbox.EPOCH_DIR_PREFIX}{index.uidvalidity}")
        failed, lost = _restore_unit(kv, prefix, index, target)
    else:
        if local_epoch is None and index.uidvalidity is not None:
            os.makedirs(list_dir, exist_ok=True)
            _atomic_write(
                os.path.join(list_dir, mbox.UIDVALIDITY_FILE),
                index.uidvalidity.encode("ascii"),
            )
        failed, lost = _restore_unit(kv, prefix, index, list_dir)
    for name in kv.list_children(prefix):
        match = _EPOCH_DIR.match(name)
        # An archive of the epoch the local cache is now in would only duplicate it.
        if not match or match.group(1) == local_epoch:
            continue
        unit_prefix = f"{prefix}{name}/"
        try:
            unit_index = _load_imap_index(kv, unit_prefix + _IMAP_INDEX)
        except _CorruptIndex:
            lost += 1
            continue
        except Exception:  # pylint: disable=broad-except
            failed += 1
            continue
        more_failed, more_lost = _restore_unit(
            kv, unit_prefix, unit_index, os.path.join(list_dir, name)
        )
        failed += more_failed
        lost += more_lost
    return failed, lost


def _restore_unit(
    kv: KvStore, prefix: str, index: _ImapIndex, unit_dir: str
) -> Tuple[int, int]:
    """Fetch the chunks of one stored unit that `unit_dir` doesn't already hold."""
    local = _local_chunks(unit_dir)
    failed = lost = 0
    for chunk, uids in sorted(index.chunks.items()):
        # A warm host already holding the chunk's mail fetches nothing.
        if uids <= local.get(chunk, set()):
            continue
        try:
            record = kv.get(_chunk_key(prefix, chunk))
        except Exception:  # pylint: disable=broad-except
            failed += 1
            continue
        if record is None:
            lost += 1
            continue
        try:
            members = _unpack_chunk(record[0])
        except (tarfile.TarError, EOFError, OSError, zlib.error):
            lost += 1
            continue
        for uid, data in members.items():
            path = os.path.join(unit_dir, f"{uid}.eml")
            if not os.path.exists(path):
                _atomic_write(path, data)
    return failed, lost


def _persist_imap(kv: KvStore, corpus: str, problems: List[str]) -> None:
    """Upload each local list's mail as chunks. Never raises; a failure on one
    list never skips the rest, and is reported in `problems` (a persistent one
    means every fresh container does a full refetch)."""
    prefix = _IMAP_PREFIX.format(corpus=corpus)
    root = _imap_root(corpus)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        list_dir = os.path.join(root, name)
        if not os.path.isdir(list_dir):
            continue
        try:
            skipped = _persist_imap_list(kv, f"{prefix}{name}/", list_dir)
        except Exception as err:  # pylint: disable=broad-except
            problems.append(f"IMAP mail cache for '{name}' not persisted ({err})")
            continue
        if skipped:
            problems.append(f"IMAP mail cache for '{name}' not persisted ({skipped})")


def _persist_imap_list(kv: KvStore, prefix: str, list_dir: str) -> Optional[str]:
    """Upload one list: its archive units, then its main unit. Returns why the
    main unit was deliberately left alone, or `None`."""
    stored = _load_imap_index(kv, prefix + _IMAP_INDEX)
    epoch = mbox.read_uidvalidity(list_dir)
    # Replace the stored mail only when this gather's sync confirmed the local
    # epoch against the server: epochs are labels, not an order, so a differing
    # one could just as well be a stale cache on a host whose sync failed, and
    # the store may hold a newer epoch's mail (including mail outside the
    # window, which exists nowhere else).
    superseded = _epochs_differ(epoch, stored.uidvalidity)
    if superseded and mbox.verified_uidvalidity(list_dir) != epoch:
        return "local epoch differs from the stored one and was not confirmed"
    # Archives first. When the main unit is about to be replaced, the old
    # epoch's mail must be safely stored as an archive before the stored copy
    # goes. Normally the local `epoch-<old>/` dir already holds it (the sync moved
    # it there, or hydrate restored it there); fill in anything it lacks from the
    # stored main unit so this doesn't depend on that. A failure to fetch it, or
    # to store the archive, abandons the list with the stored main unit untouched.
    if superseded and stored.chunks:
        archive_dir = os.path.join(
            list_dir, f"{mbox.EPOCH_DIR_PREFIX}{stored.uidvalidity}"
        )
        failed, _ = _restore_unit(kv, prefix, stored, archive_dir)
        if failed:
            raise OSError(
                f"{failed} stored chunk(s) of the old epoch could not be fetched "
                "to archive"
            )
    for name in sorted(os.listdir(list_dir)):
        match = _EPOCH_DIR.match(name)
        unit_dir = os.path.join(list_dir, name)
        # An archive of the epoch the cache is now in (a restore put it there
        # from the stored main unit) would only duplicate that unit.
        if not match or not os.path.isdir(unit_dir) or match.group(1) == epoch:
            continue
        unit_prefix = f"{prefix}{name}/"
        unit_stored = _load_imap_index(kv, unit_prefix + _IMAP_INDEX)
        _persist_unit(kv, unit_prefix, unit_dir, None, unit_stored)
    _persist_unit(kv, prefix, list_dir, epoch, stored)
    return None


def _persist_unit(
    kv: KvStore, prefix: str, unit_dir: str, epoch: Optional[str], stored: _ImapIndex
) -> None:
    """Upload one unit's chunks and index. `stored` is what the store holds for
    it; a unit whose stored epoch differs from `epoch` is replaced wholesale."""
    local = _local_chunks(unit_dir)
    if not local:
        return
    superseded = _epochs_differ(epoch, stored.uidvalidity)
    base = {} if superseded else stored.chunks
    chunks = dict(base)
    index_key = prefix + _IMAP_INDEX
    if superseded:
        # Invalidate the old index before touching its chunks: a failure part
        # way through then leaves an index naming no chunks, never one naming
        # chunks whose contents belong to another epoch.
        kv.put(index_key, _dump_imap_index(_ImapIndex(epoch, {})))
    for chunk, uids in sorted(local.items()):
        have = base.get(chunk, set())
        if uids == have:
            continue
        members: Dict[int, bytes] = {}
        for uid in uids:
            data = _read(os.path.join(unit_dir, f"{uid}.eml"))
            if data is not None:
                members[uid] = data
        if not uids >= have:
            # The store holds mail this host lacks (a partial hydrate, say):
            # fold it in rather than overwrite it with less. A failed fetch
            # raises, abandoning the list with the stored chunk untouched.
            record = kv.get(_chunk_key(prefix, chunk))
            if record is not None:
                for uid, data in _unpack_chunk(record[0]).items():
                    members.setdefault(uid, data)
        kv.put(_chunk_key(prefix, chunk), _pack_chunk(members))
        chunks[chunk] = set(members)
    index = _ImapIndex(epoch or stored.uidvalidity, chunks)
    if superseded or index != stored:
        # Chunks first, index last: a crash in between leaves chunks the index
        # doesn't list yet, which the next persist re-covers.
        kv.put(index_key, _dump_imap_index(index))
    for chunk in stored.chunks.keys() - chunks.keys():
        kv.delete(_chunk_key(prefix, chunk))


# --- local file helpers ---------------------------------------------------


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    data = _read(path)
    if data is None:
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
