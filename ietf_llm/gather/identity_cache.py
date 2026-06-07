"""Shared on-disk plumbing for the gather layer's identity-map caches
(`_github-users.json`, `_datatracker-github.json`).

Each cache module owns its path, its load semantics (they differ — one drops
non-dict entries, the other keeps `None` "confirmed-absent" markers), and its
merge function. This owns the atomic write and the locked reload-merge-save that
keeps the runner's concurrent same-process gathers from clobbering each other's
local additions before they reach the store (issue #82 review).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict

#: A `remote, local -> merged` union supplied by each cache module.
MergeFn = Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
#: A no-arg reload of a cache's on-disk file, with that cache's own semantics.
LoadFn = Callable[[], Dict[str, Any]]


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
    load: LoadFn,
    merge: MergeFn,
    cache: Dict[str, Any],
) -> None:
    """Reload the on-disk cache (via the module's own `load`) and merge `cache`
    into it under `lock`, then write atomically. Lossless against a concurrent
    same-process gather: a plain load-modify-save would drop additions made since
    `cache` was snapshotted."""
    with lock:
        save(path, merge(load(), cache))
