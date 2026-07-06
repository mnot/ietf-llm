"""TTL-cached Datatracker fetch layer shared by the live-lookup readers.

The single network seam (`_fetch_json`) plus the caching around it: a small
in-process TTL cache and a best-effort cross-process copy on disk
(`.live-cache.json` under the cache root), so a cold, short-lived process
does not re-hit Datatracker on every call. Also holds the primitives the
meetings / drafts readers share — the base URLs, the datetime helpers
(`_now_utc` / `_parse_utc`), and the `age_stamp` freshness footer.

Writes nothing but the disk cache: no ETag store, never any corpus content,
so a re-gather remains the only thing that mutates a corpus, and on a
read-only cache mount the disk write silently no-ops. Tests stub the one
network seam via `live_lookup.cache._fetch_json`.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

from ..atomicio import atomic_open, file_lock
from ..net import DEFAULT_HEADERS, governed_get
from ..paths import get_cache_dir

_DT_BASE = "https://datatracker.ietf.org"
_API_BASE = f"{_DT_BASE}/api/v1"

#: Default TTL for the in-process live cache (seconds). Short — these facts
#: move daily, and the point of the cache is only to coalesce a burst of
#: reads in one agenda-building session, not to serve stale data. Override
#: with `IETF_LLM_LIVE_TTL`.
_DEFAULT_TTL = 300.0


def _ttl_seconds() -> float:
    """The live cache TTL from `IETF_LLM_LIVE_TTL` (seconds; default 300).

    A non-numeric or negative value falls back to the default.
    """
    raw = os.environ.get("IETF_LLM_LIVE_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL
    try:
        ttl = float(raw)
    except ValueError:
        return _DEFAULT_TTL
    return ttl if ttl >= 0 else _DEFAULT_TTL


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO timestamp to a tz-aware UTC datetime (Z or naive = UTC)."""
    raw = (value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


@dataclass
class _CacheEntry:
    body: Optional[Dict[str, Any]]
    fetched_at: datetime.datetime
    monotonic: float


_cache: Dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _fetch_json(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """GET a Datatracker JSON URL, returning the decoded dict or None.

    No disk cache and no ETag store — the read path writes nothing. Uses the
    shared rate governor (`governed_get`) so a burst of agenda lookups stays
    polite to Datatracker. Any transport or decode error is swallowed to
    None; the caller decides whether to fall back to a stale cache entry.
    """
    try:
        response = governed_get(url, headers=dict(DEFAULT_HEADERS), timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _live_cache_path() -> str:
    return os.path.join(get_cache_dir(), ".live-cache.json")


def _disk_get(
    url: str, ttl: Optional[float]
) -> Optional[Tuple[Dict[str, Any], datetime.datetime, float]]:
    """Disk-cached `(body, fetched_at, epoch)` for `url`, or None.

    `ttl` is the freshness window; pass None to accept a stale entry (the
    fetch-failure fallback, where a stale answer beats none). A non-dict body
    is a miss, not a `None` hit — a corrupt or hand-edited entry must not
    masquerade as an empty result. Best-effort: any read/parse error is a miss.
    """
    try:
        with open(_live_cache_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = data.get(url) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    epoch = entry.get("epoch")
    if not isinstance(epoch, (int, float)):
        return None
    if ttl is not None and (time.time() - epoch) >= ttl:
        return None
    body = entry.get("body")
    if not isinstance(body, dict):
        return None
    try:
        fetched_at = datetime.datetime.fromisoformat(str(entry.get("fetched_at")))
    except ValueError:
        fetched_at = _now_utc()
    return body, fetched_at, float(epoch)


def _disk_put(
    url: str, body: Dict[str, Any], fetched_at: datetime.datetime, ttl: float
) -> None:
    """Store a fresh entry on disk, pruning expired ones. Best-effort and
    serialised across processes by a file lock so concurrent CLI invocations
    do not clobber each other; any OS error (e.g. a read-only mount) no-ops."""
    path = _live_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with file_lock(path + ".lock"):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            now = time.time()
            fresh = {
                key: val
                for key, val in data.items()
                if isinstance(val, dict)
                and isinstance(val.get("epoch"), (int, float))
                and (now - val["epoch"]) < ttl
            }
            fresh[url] = {
                "body": body,
                "fetched_at": fetched_at.isoformat(),
                "epoch": now,
            }
            with atomic_open(path) as handle:
                json.dump(fresh, handle)
    except OSError:
        pass


def _cached_json(url: str) -> Tuple[Optional[Dict[str, Any]], datetime.datetime]:
    """Return `(body, fetched_at)` for `url`, fetching at most once per TTL.

    Checks the in-process cache first, then the cross-process disk cache (so a
    cold process reuses a recent fetch rather than re-hitting Datatracker), then
    fetches. On a fetch failure a previously-cached body is returned with its
    *original* timestamp — in-process, else a stale disk entry — since a stale
    answer beats none; with no prior entry anywhere, `(None, now)`.
    """
    ttl = _ttl_seconds()
    now_mono = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry is not None and (now_mono - entry.monotonic) < ttl:
            return entry.body, entry.fetched_at
    disk = _disk_get(url, ttl)
    if disk is not None:
        disk_body, disk_fetched, disk_epoch = disk
        # Preserve the disk entry's real age so the in-process TTL check does
        # not restart the clock — otherwise a near-expired disk datum would be
        # served for a further full TTL (up to ~2×TTL total).
        age = max(0.0, time.time() - disk_epoch)
        with _cache_lock:
            _cache[url] = _CacheEntry(
                body=disk_body,
                fetched_at=disk_fetched,
                monotonic=time.monotonic() - age,
            )
        return disk_body, disk_fetched
    body = _fetch_json(url)
    if body is None:
        # Fetch failed: prefer any stale answer over none. In-process first,
        # then a stale disk entry — the cold-process case the disk cache exists
        # for (a fresh process, network down, only older-than-TTL data on disk).
        with _cache_lock:
            entry = _cache.get(url)
        if entry is not None:
            return entry.body, entry.fetched_at
        stale = _disk_get(url, None)
        if stale is not None:
            return stale[0], stale[1]
        return None, _now_utc()
    fetched_at = _now_utc()
    with _cache_lock:
        _cache[url] = _CacheEntry(
            body=body, fetched_at=fetched_at, monotonic=time.monotonic()
        )
    _disk_put(url, body, fetched_at, ttl)
    return body, fetched_at


def _reset_cache() -> None:
    """Drop the in-process cache. For tests."""
    with _cache_lock:
        _cache.clear()


def age_stamp(fetched_at: datetime.datetime) -> str:
    """A one-line freshness footer for a live result.

    Reports the UTC fetch time and how long ago it was, so a caller can tell
    a just-fetched answer from one served out of the short TTL cache (or a
    stale-on-failure fallback).
    """
    age = max(0, int((_now_utc() - fetched_at).total_seconds()))
    stamp = fetched_at.strftime("%Y-%m-%d %H:%M:%SZ")
    if age <= 1:
        return f"_Live from Datatracker, fetched just now ({stamp})._"
    return (
        f"_Live from Datatracker, fetched {age}s ago ({stamp}); "
        f"cached up to {int(_ttl_seconds())}s._"
    )
