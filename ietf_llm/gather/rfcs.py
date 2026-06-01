"""Mirror the RFC-series index from rfc.fyi into the local cache.

rfc.fyi already publishes the canonical, edge-cached JSON for the whole
RFC series (`rfcs.json`, `refs.json`, `tags.json`), rebuilt from
`rfc-index.xml` + the reference graph + curated collections. Rather than
re-run that pipeline we mirror its output into
`~/.cache/ietf-llm/_rfc/`, where `rfcs.RfcData` reads it network-free.

This is a singleton, not a per-corpus artifact: `ensure_rfc_index` runs
once per `ietf-llm` invocation (see `__main__.main`), invisibly. It is
cheap to call:

  - **TTL guard.** If the local copy is younger than `RFC_TTL_SECONDS`
    we don't touch the network at all.
  - **Conditional GET.** Past the TTL we revalidate with `If-None-Match`
    from a `.etag` sidecar; an unchanged file comes back 304 with no
    body, and we just touch the local file's mtime to restart the TTL.
  - **gzip** is negotiated automatically by `requests` (we don't set
    `Accept-Encoding`), so a real 200 transfers compressed.

Sidecar hygiene: the body is written first (atomic temp+rename), then
the `.etag` — so an etag never points at a body we failed to write. On a
200 with no ETag header we delete any stale sidecar, keeping the pair
consistent. The reader ignores sidecars entirely; the `.json` is the
source of truth.

Best-effort throughout: any network or write failure leaves the existing
cache in place and logs at PROGRESS. A missing index is not an error —
the reader degrades to a "not gathered yet" message.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import requests

from ..rfcs import RFC_FILES, rfc_index_dir
from ..utils import DEFAULT_HEADERS, LogLevel, Verbosity, log

RFC_DATA_BASE = "https://rfc.fyi/var"

#: Revalidate at most once per day. The series changes slowly and the
#: 304 path is cheap, so this is just to avoid a needless request on
#: back-to-back gathers (notably `ietf-llm --all`).
RFC_TTL_SECONDS = 24 * 60 * 60

_TIMEOUT = 30


def ensure_rfc_index(
    verbosity: Verbosity = Verbosity.STATUS, force: bool = False
) -> None:
    """Refresh the local RFC index mirror if stale. Never raises."""
    target_dir = rfc_index_dir()
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as err:
        log(
            f"RFC index: cannot create {target_dir}: {err}",
            verbosity,
            LogLevel.PROGRESS,
        )
        return
    for name in RFC_FILES:
        _refresh_one(target_dir, name, verbosity, force)


def _refresh_one(target_dir: str, name: str, verbosity: Verbosity, force: bool) -> None:
    body_path = os.path.join(target_dir, name)
    etag_path = body_path + ".etag"
    if not force and _is_fresh(body_path):
        return
    headers = dict(DEFAULT_HEADERS)
    etag = _read_etag(etag_path) if os.path.exists(body_path) else None
    if etag:
        headers["If-None-Match"] = etag
    url = f"{RFC_DATA_BASE}/{name}"
    try:
        response = requests.get(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if response.status_code == 304:
        _touch(body_path)
        return
    try:
        response.raise_for_status()
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if not _write_body(body_path, response.content, verbosity):
        return
    _write_sidecar(etag_path, response.headers.get("ETag"))
    log(f"RFC index: updated {name}", verbosity, LogLevel.PROGRESS)


def _is_fresh(body_path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(body_path)
    except OSError:
        return False
    return age < RFC_TTL_SECONDS


def _read_etag(etag_path: str) -> Optional[str]:
    try:
        with open(etag_path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


def _touch(path: str) -> None:
    # Restart the TTL after a successful 304 revalidation, so a fresh
    # check costs one conditional request per day, not one per gather.
    try:
        os.utime(path, None)
    except OSError:
        pass


def _write_body(path: str, content: bytes, verbosity: Verbosity) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
        return True
    except OSError as err:
        log(f"RFC index: write {path} failed: {err}", verbosity, LogLevel.PROGRESS)
        _unlink(tmp)
        return False


def _write_sidecar(etag_path: str, etag: Optional[str]) -> None:
    # Body is already on disk. If the server gave no ETag, drop any stale
    # sidecar so we never send a mismatched If-None-Match next time.
    if not etag:
        _unlink(etag_path)
        return
    tmp = etag_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(etag)
        os.replace(tmp, etag_path)
    except OSError:
        _unlink(tmp)


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
