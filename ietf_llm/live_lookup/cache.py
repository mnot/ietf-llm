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
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import quote

import requests

from ..atomicio import atomic_open, file_lock
from ..log import LogLevel, log
from ..net import DEFAULT_HEADERS, governed_get, http_error_detail
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


def document_url(name: str) -> Optional[str]:
    """The API URL for one Datatracker document, or None for a name that would
    not stay one path segment (`.` and `..` survive percent-encoding and are
    collapsed by the HTTP client)."""
    if not name or set(name) == {"."}:
        return None
    return f"{_API_BASE}/doc/document/{quote(name, safe='')}/?format=json"


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


class LiveUnavailable(Exception):
    """Datatracker could not be reached and no cached answer was on hand.

    Distinct from "no such document": callers must never render this as a
    negative. `tried_at` is when the failed attempt was made.
    """

    def __init__(self, tried_at: datetime.datetime) -> None:
        super().__init__("Datatracker unreachable")
        self.tried_at = tried_at


@dataclass
class _CacheEntry:
    body: Dict[str, Any]
    fetched_at: datetime.datetime
    monotonic: float


_cache: Dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


#: `(connect, read)` timeouts for a live fetch, in seconds. There is no retry
#: (see `_fetch_json`), so these bound a tool call: an unreachable host costs
#: at most ~5s to connect, a stalled response ~10s.
_FETCH_TIMEOUT: Tuple[float, float] = (5.0, 10.0)


def _fetch_json(
    url: str, timeout: Union[float, Tuple[float, float]] = _FETCH_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """GET a Datatracker JSON URL: the decoded dict, None for a 404, else raise.

    None means Datatracker answered that the resource does not exist — the
    only status that does. Any other failure (transport error, 5xx, 429, a
    Cloudflare block, an undecodable or non-object body) is logged, with the
    Cloudflare Ray ID when the response carries one, and re-raised as
    `requests.RequestException` / `ValueError` for `_cached_json` to turn into
    "unavailable" or a stale fallback.

    No disk cache and no ETag store — the read path writes nothing. Uses the
    shared rate governor (`governed_get`) so a burst of agenda lookups stays
    polite to Datatracker, but with `retrying=False`: a caller is waiting, and
    the shared session's retries (honouring `Retry-After`) can hold a call for
    a minute or more. A failure is cheap to report ("could not reach") and the
    stale-cache fallback in `_cached_json` already covers the blip.
    """
    try:
        response = governed_get(
            url, retrying=False, headers=dict(DEFAULT_HEADERS), timeout=timeout
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            return None
        _log_fetch_failure(url, err)
        raise
    except (requests.RequestException, ValueError) as err:
        _log_fetch_failure(url, err)
        raise
    return body


def _log_fetch_failure(url: str, err: Exception) -> None:
    log(
        f"Datatracker: fetch {url} failed: {http_error_detail(err)}",
        level=LogLevel.WARN,
    )


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
                    loaded = json.load(handle)
            except (OSError, ValueError):
                loaded = None
            data = loaded if isinstance(loaded, dict) else {}
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


def _stale_entry(
    url: str,
) -> Optional[Tuple[Dict[str, Any], datetime.datetime]]:
    """Any previously-cached `(body, fetched_at)` for `url`, however old.

    The fetch-failure fallback: in-process first, then a stale disk entry — the
    cold-process case the disk cache exists for (a fresh process, network
    down, only older-than-TTL data on disk).
    """
    with _cache_lock:
        entry = _cache.get(url)
    if entry is not None:
        return entry.body, entry.fetched_at
    stale = _disk_get(url, None)
    return (stale[0], stale[1]) if stale is not None else None


def _cached_json(
    url: str,
) -> Tuple[Optional[Dict[str, Any]], datetime.datetime, bool]:
    """Return `(body, fetched_at, answered)` for `url`, fetching at most once
    per TTL.

    `answered` is False only when the fetch failed and there was no cached
    entry, fresh or stale, to fall back on: `body` is then None because
    Datatracker could not be asked, not because the resource is absent, and
    `fetched_at` is the time of the failed attempt. A 404 is an answer
    (`body` None, `answered` True), and is not cached.

    Checks the in-process cache first, then the cross-process disk cache (so a
    cold process reuses a recent fetch rather than re-hitting Datatracker), then
    fetches. On a fetch failure a previously-cached body is returned with its
    *original* timestamp (`_stale_entry`) since a stale answer beats none.
    """
    ttl = _ttl_seconds()
    now_mono = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry is not None and (now_mono - entry.monotonic) < ttl:
            return entry.body, entry.fetched_at, True
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
        return disk_body, disk_fetched, True
    try:
        body = _fetch_json(url)
    except (requests.RequestException, ValueError):
        stale = _stale_entry(url)
        if stale is not None:
            return stale[0], stale[1], True
        return None, _now_utc(), False
    fetched_at = _now_utc()
    if body is None:
        return None, fetched_at, True
    with _cache_lock:
        _cache[url] = _CacheEntry(
            body=body, fetched_at=fetched_at, monotonic=time.monotonic()
        )
    _disk_put(url, body, fetched_at, ttl)
    return body, fetched_at, True


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


def _age_phrase(when: datetime.datetime) -> str:
    age = max(0, int((_now_utc() - when).total_seconds()))
    return "just now" if age <= 1 else f"{age}s ago"


def unreachable_note(what: str, tried_at: datetime.datetime) -> str:
    """The message for a lookup Datatracker could not be asked.

    Worded so it cannot be read as a negative about `what`: an outage says
    nothing about whether the thing exists or has any reviews, positions or
    sessions.
    """
    stamp = tried_at.strftime("%Y-%m-%d %H:%M:%SZ")
    return (
        f"Could not reach Datatracker to look up {what} (last tried "
        f"{_age_phrase(tried_at)}, {stamp}). That is an outage, not an answer: "
        "nothing here says the lookup would come back empty. Try again shortly."
    )
