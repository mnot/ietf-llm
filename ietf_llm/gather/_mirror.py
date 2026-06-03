"""Shared plumbing for the cross-corpus singleton mirrors.

Both `gather.rfcs` (the `_rfc/` mirror of rfc.fyi) and `gather.catalog`
(the `_catalog/` mirror of the Datatracker group list) are the same kind
of thing: a TTL-guarded, ETag-revalidated, atomically-written best-effort
file mirror. These primitives are that shared machinery — kept in one
place so the two mirrors can't drift apart (and so pylint doesn't see two
copies of it).

Every function here is best-effort: a failed write or stat is logged at
PROGRESS and degrades gracefully, never raised. Atomicity is temp-file +
`os.replace`; the body is always written before its `.etag` sidecar, so
an etag never points at a body we failed to write.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..utils import LogLevel, Verbosity, log


def is_fresh(path: str, ttl_seconds: float) -> bool:
    """True if `path` exists and is younger than `ttl_seconds`."""
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    return age < ttl_seconds


def read_etag(etag_path: str) -> Optional[str]:
    """The stored ETag for conditional revalidation, or None."""
    try:
        with open(etag_path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


def touch(path: str) -> None:
    """Restart a file's TTL after a clean 304 revalidation, so a fresh
    check costs one conditional request per TTL window, not one per run."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def write_body(path: str, content: bytes, verbosity: Verbosity, what: str) -> bool:
    """Atomically write `content` to `path`. `what` names the mirror for
    the failure log (e.g. "RFC index", "Catalog"). Returns success."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
        return True
    except OSError as err:
        log(f"{what}: write {path} failed: {err}", verbosity, LogLevel.PROGRESS)
        unlink(tmp)
        return False


def write_sidecar(etag_path: str, etag: Optional[str]) -> None:
    """Persist `etag` beside an already-written body. With no ETag, drop
    any stale sidecar so we never send a mismatched If-None-Match next."""
    if not etag:
        unlink(etag_path)
        return
    tmp = etag_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(etag)
        os.replace(tmp, etag_path)
    except OSError:
        unlink(tmp)


def unlink(path: str) -> None:
    """Best-effort remove; missing file is fine."""
    try:
        os.remove(path)
    except OSError:
        pass
