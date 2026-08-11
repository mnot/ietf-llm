"""Mirror the RFC-series index from rfc.fyi into the local cache.

rfc.fyi already publishes the canonical, edge-cached JSON for the whole
RFC series (`rfcs.json`, `refs.json`, `tags.json`), rebuilt from
`rfc-index.xml` + the reference graph + curated collections. Rather than
re-run that pipeline we mirror its output into
`~/.cache/ietf-llm/_rfc/`, where `rfcs.RfcData` reads it network-free.

Two entry points write that mirror:

  - `ensure_rfc_index` — the gather path. A singleton, not a per-corpus
    artifact: it runs once per `ietf-llm` invocation (see `cli.main.main`),
    invisibly, and refreshes all three files.
  - `revalidate_index` — the read path, for a `get_rfc_info` miss against a
    mirror too old to trust. Gated on `gather_enabled`, throttled, bounded,
    and `rfcs.json` only. See its docstring.

`ensure_rfc_index` is cheap to call:

  - **TTL guard.** If the local copy is younger than `RFC_TTL_SECONDS`
    (defined with the reader, in `singletons.rfcs`, which uses the same
    threshold to decide when a miss is too old to trust) we don't touch
    the network at all.
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
import threading
import time
from typing import Optional

import requests

from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get
from ...singletons.rfcs import RFC_FILES, RFC_TTL_SECONDS, rfc_index_dir
from . import _mirror

RFC_DATA_BASE = "https://rfc.fyi/var"

_TIMEOUT = 30

#: Per-attempt connect/read timeout for the read path's revalidation (see
#: `revalidate_index`). Short, and paired with `retrying=False` in
#: `_refresh_one`: a caller is waiting on this, unlike a gather, and a
#: `timeout` alone is not a deadline — with the retrying session a host
#: answering `429 Retry-After: 30` would sleep ~90s past it.
_REVALIDATE_TIMEOUT = 5

#: Back off this long after a revalidation *attempt*, so a burst of misses
#: (or a down host) can't hammer rfc.fyi. Success and 304 both restart the
#: mirror's own TTL, so in practice this only governs the failure path.
_REVALIDATE_BACKOFF = 3600.0

#: Existence lives in rfcs.json alone; refs/tags are the reference graph,
#: which a miss doesn't need and which lags upstream anyway.
_EXISTENCE_FILE = "rfcs.json"

_lock = threading.Lock()
_LAST_ATTEMPT: Optional[float] = None
_IN_FLIGHT = False


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


def revalidate_index() -> None:
    """Bring `rfcs.json` up to date now, for a read that would otherwise
    answer from a mirror too old to trust. Never raises.

    This is the sanctioned networked-read exception (`gather_enabled` —
    as with the seed catalog and the live Datatracker lookups), so a
    read-only HTTP replica never fetches and keeps its offline boundary.

    Deliberately *not* stale-while-revalidate, unlike the seed catalog: a
    stale catalog is still a useful catalog, but here staleness is exactly
    what makes the answer wrong, so revalidating in the background would
    leave this very call answering "no such RFC" and only fix the retry.
    The caller waits instead. Cheap because only a *stale miss* gets here:
    a hit, or any miss inside the TTL, never calls it.

    That wait is kept short by fetching with `retrying=False` — a `timeout`
    alone is not a deadline, and the retrying session would sleep through a
    `Retry-After` far past it. Not retrying is the right trade here anyway:
    we have a correct answer to fall back on (the honest stale miss), so a
    caller is better served by it now than by a slow success. Bounded by the
    connect + read timeouts, plus any wait for the per-host slot.

    Fetches only `rfcs.json` (existence); the reference graph stays on the
    gather path. Throttled and single-flighted — when a revalidation is
    already running or one was attempted recently, we return without
    fetching and the caller falls back to the stale answer, which
    `singletons.rfcs.no_such_rfc` reports honestly.
    """
    # pylint: disable-next=import-outside-toplevel
    from ...freshness import gather_enabled

    if not gather_enabled():
        return
    target_dir = rfc_index_dir()
    # Before claiming an attempt: marking one would makedirs the mirror into
    # existence, and a read must never materialise cache. Nothing mirrored
    # yet is a gather, not a revalidation.
    if not os.path.isdir(target_dir):
        return
    if not _begin_attempt():
        return
    try:
        _refresh_one(
            target_dir,
            _EXISTENCE_FILE,
            Verbosity.QUIET,
            force=True,
            live=True,
        )
    finally:
        _end_attempt()


def reset_state() -> None:
    """Clear the in-process throttle / single-flight state. For tests, which
    reuse one process across many isolated caches (the disk marker resets with
    the cache dir, but these module globals would leak)."""
    global _LAST_ATTEMPT, _IN_FLIGHT  # pylint: disable=global-statement
    with _lock:
        _LAST_ATTEMPT = None
        _IN_FLIGHT = False


def _attempt_marker() -> str:
    return os.path.join(rfc_index_dir(), f".{_EXISTENCE_FILE}.revalidated")


def _begin_attempt() -> bool:
    """Claim the right to revalidate: single-flight, and at most one attempt
    per backoff window. Throttled in-process (which survives a cache dir we
    cannot write the marker into) and on disk (which holds across processes,
    and across a fetch that failed without touching the mirror)."""
    global _LAST_ATTEMPT, _IN_FLIGHT  # pylint: disable=global-statement
    now = time.monotonic()
    with _lock:
        if _IN_FLIGHT:
            return False
        if _LAST_ATTEMPT is not None and now - _LAST_ATTEMPT < _REVALIDATE_BACKOFF:
            return False
        if _mirror.is_fresh(_attempt_marker(), _REVALIDATE_BACKOFF):
            _LAST_ATTEMPT = now  # memoise so we don't stat the disk every miss
            return False
        _LAST_ATTEMPT = now
        _IN_FLIGHT = True
    _mark_attempt()
    return True


def _end_attempt() -> None:
    global _IN_FLIGHT  # pylint: disable=global-statement
    with _lock:
        _IN_FLIGHT = False


def _mark_attempt() -> None:
    path = _attempt_marker()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError:
        pass


def _refresh_one(
    target_dir: str,
    name: str,
    verbosity: Verbosity,
    force: bool,
    live: bool = False,
) -> None:
    """Refresh one mirrored file. `live` marks the read path — someone is
    waiting, so use the short timeout and don't retry (see `governed_get`)."""
    body_path = os.path.join(target_dir, name)
    etag_path = body_path + ".etag"
    if not force and _mirror.is_fresh(body_path, RFC_TTL_SECONDS):
        return
    headers = dict(DEFAULT_HEADERS)
    etag = _mirror.read_etag(etag_path) if os.path.exists(body_path) else None
    if etag:
        headers["If-None-Match"] = etag
    url = f"{RFC_DATA_BASE}/{name}"
    try:
        response = governed_get(
            url,
            headers=headers,
            timeout=_REVALIDATE_TIMEOUT if live else _TIMEOUT,
            retrying=not live,
        )
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if response.status_code == 304:
        _mirror.touch(body_path)
        return
    try:
        response.raise_for_status()
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if not _mirror.write_body(body_path, response.content, verbosity, "RFC index"):
        return
    _mirror.write_sidecar(etag_path, response.headers.get("ETag"))
    log(f"RFC index: updated {name}", verbosity, LogLevel.PROGRESS)
