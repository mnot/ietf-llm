"""The conditional-GET (ETag) store behind the Datatracker client.

The Datatracker JSON API returns ETags and honours If-None-Match (304). We
persist {url: etag, body} so re-gathers revalidate cheaply: an unchanged
endpoint comes back as an empty 304 and we reuse the cached body. This is the
bulk of the per-gather metadata chatter (document lists, material revisions,
the meeting batch, roles).

On disk a store is a directory: one gzip file per cached body, named by a hash
of the URL and ETag, plus a small `index.json` mapping each URL to its ETag, its
body file, that file's size, and when it was last used. A gather loads the index
and reads only the bodies it revalidates; a single JSON blob holding every body
was ~100 MB parsed in full on the first request and rewritten in full at exit.
The index is keyed by URL, so two stores can be merged as a union.

Eviction (applied at flush) keeps the store from growing without bound as many
WGs are gathered and paged URLs (id__in= chunks, &offset= pages) mint many
distinct keys. Every get() bumps the entry's last_used -- even on a 304
revalidation, since get() runs before the request -- so an endpoint that is
still being gathered, however infrequently, never ages out; only genuinely
abandoned URLs go stale. The count and byte caps are backstops against
pathological growth and keep the most-recently-used.
"""

from __future__ import annotations

import atexit
import gzip
import hashlib
import json
import os
import re
import threading
import time
import zlib
from typing import Any, Dict, Optional, Set, Tuple

from ...log import LogLevel, log

#: The index file inside a store directory.
INDEX_NAME = "index.json"
#: A body file's name: the SHA-256 of `<url>\0<etag>`. The ETag is in the hash so
#: a fresh body never overwrites the one the on-disk index still describes.
# `\Z`, not `$`: `$` also matches before a trailing newline.
BODY_NAME = re.compile(r"^[0-9a-f]{64}\.gz\Z")
_INDEX_VERSION = 1

#: Drop entries not requested within this many days (a generous window so an
#: infrequently-gathered WG keeps its cache and doesn't re-download).
MAX_AGE_DAYS = 90
#: Hard cap on entry count, enforced after the age sweep (LRU: newest kept).
MAX_ENTRIES = 10000
#: Hard cap on the compressed bodies' total size, enforced the same way.
MAX_BYTES = 256 * 1024 * 1024
#: What an archive of a store may hold beyond `MAX_BYTES` of bodies: its index.
INDEX_ALLOWANCE = 32 * 1024 * 1024
#: Unreferenced files younger than this are left alone: another process sharing
#: the directory may have written them and not flushed its index yet.
_ORPHAN_GRACE = 86400.0


def body_file_name(url: str, etag: str) -> str:
    return hashlib.sha256(f"{url}\0{etag}".encode("utf-8")).hexdigest() + ".gz"


def _atomic_write(path: str, data: bytes) -> None:
    # Unique per writer: threads share a store directory and so can processes.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_entries(raw: Any, now: float) -> Dict[str, Dict[str, Any]]:
    """The well-formed entries of a decoded index. A name that isn't a body
    file's is dropped: the index may come from a restored archive, and the name
    is joined to a directory path."""
    entries: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return entries
    for url, entry in raw.items():
        if not isinstance(url, str) or not isinstance(entry, dict):
            continue
        etag, name = entry.get("etag"), entry.get("file")
        if not isinstance(etag, str) or not isinstance(name, str):
            continue
        if not BODY_NAME.match(name):
            continue
        size = entry.get("size")
        entries[url] = {
            "etag": etag,
            "file": name,
            "size": size if isinstance(size, int) and size >= 0 else 0,
            "last_used": _float(entry.get("last_used"), now),
        }
    return entries


def _read_index_file(directory: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(directory, INDEX_NAME), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _is_foreign(data: Optional[Dict[str, Any]]) -> bool:
    """True for an index written in a layout this version does not know."""
    version = data.get("version") if data else None
    return isinstance(version, int) and version != _INDEX_VERSION


def read_index(directory: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """The entries of the store at `directory`, or None if there is no readable
    index (never written, corrupt, or of another version)."""
    data = _read_index_file(directory)
    if data is None or data.get("version") != _INDEX_VERSION:
        return None
    return _clean_entries(data.get("entries"), time.time())


class HttpCache:
    """Lazy ETag store. The index is loaded from disk on first use and flushed
    atomically (once at interpreter exit for the process-default store, or
    explicitly when a cloud gather ends for a per-corpus store) -- so a gather
    writes its index once, not once per cached URL. A body is written when it is
    stored, to a file the on-disk index doesn't yet name, so a crash before the
    flush leaves the previous state intact. Stale and excess entries are evicted
    at flush time; see the module docstring for the policy.

    `legacy` is the path of the single-file blob an earlier version kept in place
    of `dest`; it is migrated on first load and removed at the first flush."""

    def __init__(
        self,
        dest: str,
        *,
        persist_at_exit: bool = True,
        legacy: Optional[str] = None,
    ) -> None:
        # `dest` is captured at construction, not re-resolved later: a flush may
        # be deferred to interpreter exit, and re-resolving get_cache_dir() there
        # would pick up a different cache dir if HOME changed in between (tests
        # revert their HOME monkeypatch before atexit fires).
        self._dest = dest
        self._legacy = legacy
        self._entries: Optional[Dict[str, Dict[str, Any]]] = None
        self._dirty = False
        self._registered_atexit = False
        # An index of a version this code does not understand: the store is
        # left exactly as found (empty to readers, unwritten, unswept).
        self._foreign = False
        # Body files no entry names any more; deleted once the index that no
        # longer names them is on disk.
        self._dropped: Set[str] = set()
        self._lock = threading.RLock()
        # Per-corpus stores are flushed explicitly when their gather ends
        # (`gather_http_cache`), so they skip atexit -- that keeps a dropped
        # per-corpus store collectable (an atexit-registered bound method would
        # pin it for the life of the process). The process-default store has no
        # explicit flush, so it does register atexit.
        self._persist_at_exit = persist_at_exit

    def _load(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self._entries is None:
                data = _read_index_file(self._dest)
                if _is_foreign(data):
                    log(
                        f"Datatracker: ETag store {self._dest} has an index from "
                        "another version; leaving it untouched",
                        level=LogLevel.WARN,
                    )
                    self._foreign = True
                    self._entries = {}
                elif data is not None and data.get("version") == _INDEX_VERSION:
                    self._entries = _clean_entries(data.get("entries"), time.time())
                else:
                    self._entries = self._migrate_legacy()
            return self._entries

    def _migrate_legacy(self) -> Dict[str, Dict[str, Any]]:
        """Re-store the entries of the old single-file blob, if there is one. A
        blob that is corrupt, truncated or oddly shaped yields whatever entries
        are sound (none, for unparseable JSON): a cache miss costs one refetch."""
        if not self._legacy:
            return {}
        try:
            with open(self._legacy, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        now = time.time()
        entries: Dict[str, Dict[str, Any]] = {}
        for url, raw in data.items():
            if not isinstance(url, str) or not isinstance(raw, dict):
                continue
            etag, body = raw.get("etag"), raw.get("body")
            if not isinstance(etag, str) or not isinstance(body, str):
                continue
            written = self._write_body(url, etag, body)
            if written is not None:
                entries[url] = {
                    "etag": etag,
                    "file": written[0],
                    "size": written[1],
                    "last_used": _float(raw.get("last_used"), now),
                }
        self._mark_dirty()
        return entries

    def _mark_dirty(self) -> None:
        with self._lock:
            if self._foreign:
                return
            self._dirty = True
            if self._persist_at_exit and not self._registered_atexit:
                self._registered_atexit = True
                atexit.register(self.flush)

    def _write_body(self, url: str, etag: str, body: str) -> Optional[Tuple[str, int]]:
        if self._foreign:
            return None
        name = body_file_name(url, etag)
        try:
            data = gzip.compress(body.encode("utf-8"), compresslevel=6, mtime=0)
            os.makedirs(self._dest, exist_ok=True)
            _atomic_write(os.path.join(self._dest, name), data)
        except (OSError, ValueError):
            return None
        return name, len(data)

    def _drop(self, url: str) -> None:
        entry = self._load().pop(url, None)
        if entry is not None:
            self._dropped.add(entry["file"])
            self._mark_dirty()

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        """The entry for `url` (its `etag` and `last_used`), or None. An entry
        whose body file has gone missing is dropped rather than returned: its
        ETag would earn a 304 we could not honour."""
        with self._lock:
            entry = self._load().get(url)
            if entry is None:
                return None
            if not os.path.isfile(os.path.join(self._dest, entry["file"])):
                self._drop(url)
                return None
            # Touch on read -- including the 304 path, which reuses the body
            # without a store() -- so an actively-gathered URL stays fresh.
            entry["last_used"] = time.time()
            self._mark_dirty()
            return entry

    def body(self, url: str) -> Optional[str]:
        """The cached body for `url`, or None if absent or unreadable (an
        unreadable one is dropped, so the next request fetches it afresh)."""
        with self._lock:
            entry = self._load().get(url)
            name = entry["file"] if entry else None
        if name is None:
            return None
        try:
            with open(os.path.join(self._dest, name), "rb") as fh:
                return gzip.decompress(fh.read()).decode("utf-8")
        except (OSError, EOFError, zlib.error, ValueError):
            with self._lock:
                self._drop(url)
            return None

    def store(self, url: str, etag: str, body: str) -> None:
        written = self._write_body(url, etag, body)
        if written is None:
            return
        name, size = written
        with self._lock:
            old = self._load().get(url)
            if old is not None and old["file"] != name:
                self._dropped.add(old["file"])
            self._dropped.discard(name)
            self._load()[url] = {
                "etag": etag,
                "file": name,
                "size": size,
                "last_used": time.time(),
            }
            self._mark_dirty()

    def _evict(self, now: float) -> None:
        """Drop entries unused past the age window, then cap the count and the
        total size, keeping the most recently used."""
        entries = self._entries or {}
        cutoff = now - MAX_AGE_DAYS * 86400.0
        kept: Dict[str, Dict[str, Any]] = {}
        total = 0
        ranked = sorted(
            entries.items(),
            key=lambda item: float(item[1].get("last_used", now)),
            reverse=True,
        )
        for url, entry in ranked:
            size = int(entry.get("size", 0))
            if (
                float(entry.get("last_used", now)) < cutoff
                or len(kept) >= MAX_ENTRIES
                or total + size > MAX_BYTES
            ):
                self._dropped.add(entry["file"])
                continue
            kept[url] = entry
            total += size
        self._entries = kept

    def flush(self) -> None:
        with self._lock:
            if self._entries is None or not self._dirty:
                return
            self._evict(time.time())
            index = {"version": _INDEX_VERSION, "entries": self._entries}
            try:
                os.makedirs(self._dest, exist_ok=True)
                _atomic_write(
                    os.path.join(self._dest, INDEX_NAME),
                    json.dumps(index).encode("utf-8"),
                )
            except OSError:
                return
            self._dirty = False
            self._remove_unreferenced()

    def _remove_unreferenced(self) -> None:
        """Delete the files the index just written no longer names: those this
        store dropped, and older strays (a crashed writer's, or an index
        replaced by a restore)."""
        for name in self._dropped:
            self._remove(os.path.join(self._dest, name))
        self._dropped.clear()
        referenced = {entry["file"] for entry in (self._entries or {}).values()}
        cutoff = time.time() - _ORPHAN_GRACE
        try:
            names = os.listdir(self._dest)
        except OSError:
            names = []
        for name in names:
            if name in referenced or name == INDEX_NAME:
                continue
            if not (BODY_NAME.match(name) or name.endswith(".tmp")):
                continue
            path = os.path.join(self._dest, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    self._remove(path)
            except OSError:
                pass
        if self._legacy:
            self._remove(self._legacy)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass
