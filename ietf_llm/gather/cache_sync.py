"""Round-trip the gather accelerator caches to the cloud KvStore, so an
ephemeral host (scale-to-zero) doesn't rebuild them by re-hitting rate-limited
upstreams after its scratch is wiped (issue #82).

Only the cloud CorpusStore backend calls in; the local backend keeps these
caches on persistent disk and needs no sync. The caches have different
contention profiles, so each is scoped to the unit that already guarantees a
single writer:

  - `.http-cache.json` — the datatracker ETag store. Sharded **per corpus**
    under that corpus's prefix; #85's per-corpus gather lease already serialises
    writers, so hydrate -> gather -> flush -> persist is a plain
    read-modify-write (no CAS, and no lost delta from a shared blob).
  - `_github-users.json` / `_datatracker-github.json` — corpus-independent
    identity maps, **shared** at a fleet prefix. Two different-corpus gathers can
    run at once, so persist does a bounded compare-and-swap merge: lossless, and
    cheap because contention is rare and the maps are append-mostly.
  - `_catalog/` — the `find_efforts` effort catalog (the Datatracker group
    collection — the same rate-limited upstream as the ETag store). A
    **shared fleet singleton** mirrored once per gather; every gather writes
    identical content, so last-writer-wins is correct — no CAS, no merge. A
    directory of a few files (raw source slices + the derived `catalog.json`,
    each with its `.etag` sidecar), so it round-trips as a key per file under one
    fleet prefix; the restored sidecars let the next gather revalidate (304)
    rather than re-fetch. (`_rfc/` — the same shape but a non-rate-limited CDN
    mirror — is deferred; see issue #82.)

Best-effort throughout: a sync failure must never fail a gather (it just means a
cold rebuild next time), matching how `_save_cache`/`_HttpCache.flush` already
swallow their own write errors. Each cache syncs independently, so one cache's
failure never skips the others.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional

from ..catalog import catalog_index_dir
from ..kv_store import ABSENT, KvStore
from . import datatracker, datatracker_github, github_users

#: Per-corpus key — lease-serialised single writer, so a plain RMW is safe.
_HTTP_CACHE_KEY = "corpora/{corpus}/gather-cache/http-cache.json"
#: Shared fleet keys — concurrent writers, so persist uses a CAS-merge.
_GITHUB_USERS_KEY = "fleet/gather-cache/github-users.json"
_DATATRACKER_GITHUB_KEY = "fleet/gather-cache/datatracker-github.json"
#: Shared fleet prefix for the effort-catalog directory — one key per file,
#: last-writer-wins (every gather mirrors identical upstream content).
_CATALOG_PREFIX = "fleet/catalog/"

#: Bounded retries for the shared-map compare-and-swap (mirrors `kv_control`).
_CAS_RETRIES = 8

MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]


def hydrate(kv: KvStore, corpus: str) -> None:
    """Pull the persisted caches into the local cache dir before a gather, so the
    gather revalidates (304s) and reuses identity lookups instead of re-fetching.
    Never raises."""
    _restore(kv, _HTTP_CACHE_KEY.format(corpus=corpus), datatracker.http_cache_path())
    # Rebind the process-wide ETag store to the freshly-hydrated per-corpus file,
    # so a long-lived worker doesn't carry the previous corpus's entries.
    try:
        datatracker.reset_http_cache()
    except Exception:  # pylint: disable=broad-except
        pass
    _restore(kv, _GITHUB_USERS_KEY, github_users.cache_path())
    _restore(kv, _DATATRACKER_GITHUB_KEY, datatracker_github.cache_path())
    _restore_tree(kv, _CATALOG_PREFIX, catalog_index_dir())


def persist(kv: KvStore, corpus: str) -> None:
    """Push the caches back to durable storage after a gather. Never raises."""
    # The ETag store flushes only at interpreter exit, which never fires between
    # gathers in the serve worker — force it so the file reflects this gather.
    try:
        datatracker.flush_http_cache()
    except Exception:  # pylint: disable=broad-except
        pass
    _put_plain(kv, _HTTP_CACHE_KEY.format(corpus=corpus), datatracker.http_cache_path())
    _cas_merge(
        kv, _GITHUB_USERS_KEY, github_users.cache_path(), github_users.merge_cache
    )
    _cas_merge(
        kv,
        _DATATRACKER_GITHUB_KEY,
        datatracker_github.cache_path(),
        datatracker_github.merge_cache,
    )
    _persist_tree(kv, _CATALOG_PREFIX, catalog_index_dir())


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
    is needed). Skips atomic-write `.tmp` leftovers."""
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
