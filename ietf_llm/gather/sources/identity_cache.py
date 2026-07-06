"""Shared on-disk plumbing for the gather layer's identity-map caches
(`_github-users.json`, `_datatracker-github.json`, `_datatracker-people.json`).

Each cache module owns its path and its merge function. This owns the atomic
write and the locked reload-merge-save that keeps the runner's concurrent
same-process gathers from clobbering each other's local additions before they
reach the store (issue #82 review). It also provides the common building blocks
the keep-`None` caches share — `load` (parse, keeping `None` "confirmed-absent"
markers), `union_newer_by_stamp` (the stamp-wins union their `merge_cache`
functions are built on), and `now_iso` — so those modules don't each re-spell
them. (`_github-users.json` keeps its own load/merge: it drops non-dict entries,
different semantics.)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Optional

#: A `remote, local -> merged` union supplied by each cache module.
MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
#: A no-arg reload of a cache's on-disk file, with that cache's own semantics.
LoadFn = Callable[[], Dict[str, Any]]


def load(path: str) -> Dict[str, Any]:
    """Parse a cache file, returning `{}` on any read / parse error or a
    non-dict top level. Keeps `None`-valued entries (the "confirmed-absent"
    markers the datatracker caches store), so a recorded miss survives a
    reload."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp(entry: Any) -> str:
    return str(entry.get("fetched_at", "")) if isinstance(entry, dict) else ""


def union_newer_by_stamp(
    remote: Dict[str, Any],
    local: Dict[str, Any],
    *,
    into: Optional[Dict[str, Any]] = None,
    skip: FrozenSet[str] = frozenset(),
) -> Dict[str, Any]:
    """Union two identity maps, keeping the entry with the newer `fetched_at`
    on a key both hold — so a stamped real resolution beats a bare `None` miss.
    Concurrent fleet gathers each add disjoint keys, so the union is what makes
    the round-trip lossless. `into` seeds the result for a caller that handles
    some keys specially first; `skip` names the keys to leave to that caller."""
    merged: Dict[str, Any] = dict(into) if into else {}
    for key, entry in list(remote.items()) + list(local.items()):
        if key in skip:
            continue
        if key not in merged or _stamp(entry) >= _stamp(merged[key]):
            merged[key] = entry
    return merged


def now_iso() -> str:
    """UTC timestamp in the `fetched_at` format the identity caches store."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(path: str, cache: Dict[str, Any]) -> None:
    """Persist `cache` to `path` atomically (tmp + rename, so a crash mid-write
    can't corrupt the file). Best-effort: a write failure just means the next
    gather repeats the work."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def merge_save(
    lock: threading.Lock,
    path: str,
    load_fn: LoadFn,
    merge: MergeFn,
    cache: Dict[str, Any],
) -> None:
    """Reload the on-disk cache (via the module's own loader) and merge `cache`
    into it under `lock`, then write atomically. Lossless against a concurrent
    same-process gather: a plain load-modify-save would drop additions made since
    `cache` was snapshotted."""
    with lock:
        save(path, merge(load_fn(), cache))
