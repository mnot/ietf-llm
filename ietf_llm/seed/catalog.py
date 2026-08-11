"""Local mirror of the seed store's index (issue #182), so `list_corpora` can
show what is available to fast-start.

`cached_index` is a plain **offline** read of a local mirror. `refresh_mirror`
keeps that mirror current under the gather gate (`gather_enabled` — the project's
sanctioned networked-read exception, like the live Datatracker lookups), with
**stale-while-revalidate** semantics so a routine `list_corpora` never blocks:

- **mirror present and fresh** → nothing to do;
- **present but stale** → serve the cached copy now, revalidate in a background
  thread (`list_corpora` does not wait);
- **absent** (a cold-start client with nothing gathered) → one *bounded* blocking
  fetch so this very call can show the catalog.

Throttled to at most once per `_REFRESH_TTL` — in-process (a monotonic guard that
survives a write-failing cache dir) and on disk (an `.attempt` marker, so the
throttle also holds across processes and across a failed fetch). A read-only HTTP
replica (gather off) never fetches.

Module-level imports stay read-safe (stdlib + `seed.format` + `paths`); the
network-touching `seed.fetch`, `config.service`, and `freshness` are imported
lazily, so a plain `cached_index` read pulls nothing.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from ..paths import seed_index_cache_path
from . import format as fmt

#: Refresh the mirror at most this often (seconds), so a burst of `list_corpora`
#: calls does at most one fetch and a down host is retried at most hourly.
_REFRESH_TTL = 3600.0

#: Cold miss (no mirror yet): the one bounded blocking fetch, short so a listing
#: never stalls long. A background revalidate can be more patient.
_COLD_FETCH_TIMEOUT = 3.0
_BG_FETCH_TIMEOUT = 10.0

_lock = threading.Lock()
#: Monotonic time of the last refresh attempt in this process, and whether a
#: background revalidate is running — the in-process throttle + single-flight.
_LAST_ATTEMPT: Optional[float] = None
_IN_FLIGHT = False


def refresh_mirror() -> None:
    """Bring the mirror up to date, stale-while-revalidate (see module docstring).
    Gated, throttled, and best-effort; never raises."""
    seed_url = _seed_url_if_enabled()
    if seed_url is None:
        return
    present = os.path.isfile(seed_index_cache_path())
    if present and not _mirror_stale():
        return  # fresh cache — serve it, no work
    if not _begin_attempt():
        return  # throttled or a revalidate is already in flight — serve what we have
    if present:
        _start_background(seed_url)  # SWR: serve the stale copy, revalidate async
    else:
        try:
            _fetch_and_cache(seed_url, _COLD_FETCH_TIMEOUT)  # cold: bounded block
        finally:
            _end_attempt()


def cache_index(index: fmt.Index) -> None:
    """Persist `index` to the local mirror (best-effort — a failure is ignored;
    the catalog hint is a convenience, not a correctness invariant)."""
    path = seed_index_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(index.to_json())
        os.replace(tmp, path)
    except OSError:
        pass


def cached_index() -> Optional[fmt.Index]:
    """The last-mirrored seed index, or None if never fetched / unreadable."""
    try:
        with open(seed_index_cache_path(), "r", encoding="utf-8") as handle:
            return fmt.Index.from_json(handle.read())
    except (OSError, fmt.SeedFormatError):
        return None


def reset_state() -> None:
    """Clear the in-process throttle / single-flight state. For tests, which reuse
    one process across many isolated caches (the disk markers reset with the cache
    dir, but these module globals would leak)."""
    global _LAST_ATTEMPT, _IN_FLIGHT  # pylint: disable=global-statement
    with _lock:
        _LAST_ATTEMPT = None
        _IN_FLIGHT = False


# --- internals ------------------------------------------------------------- #


def _seed_url_if_enabled() -> Optional[str]:
    # pylint: disable=import-outside-toplevel
    from ..config import service
    from ..freshness import gather_enabled

    from . import generation

    if not gather_enabled() or not service.seeding_enabled():
        return None
    return generation.store_url()


def _fetch_and_cache(seed_url: str, timeout: float) -> None:
    # pylint: disable-next=import-outside-toplevel
    from . import fetch as seed_fetch

    index = seed_fetch.load_index(seed_url, timeout=timeout)
    if index is not None:
        cache_index(index)


def _start_background(seed_url: str) -> None:
    def _run() -> None:
        try:
            _fetch_and_cache(seed_url, _BG_FETCH_TIMEOUT)
        finally:
            _end_attempt()

    # Run inline when the suite forces synchronous background work (the same
    # signal gather uses), so tests are deterministic; a real daemon thread
    # otherwise, so `list_corpora` returns without waiting.
    if os.environ.get("IETF_LLM_GATHER_INPROCESS"):
        _run()
    else:
        threading.Thread(target=_run, name="seed-catalog-refresh", daemon=True).start()


def _mirror_stale() -> bool:
    try:
        return (time.time() - os.path.getmtime(seed_index_cache_path())) >= _REFRESH_TTL
    except OSError:
        return True


def _begin_attempt() -> bool:
    """Claim the right to refresh: single-flight and throttle to <=1/TTL, both
    in-process (survives a write-failing cache) and on disk (across processes and
    across a failed fetch). Returns False when a refresh happened recently or one
    is already running."""
    global _LAST_ATTEMPT, _IN_FLIGHT  # pylint: disable=global-statement
    now = time.monotonic()
    with _lock:
        if _IN_FLIGHT:
            return False
        if _LAST_ATTEMPT is not None and now - _LAST_ATTEMPT < _REFRESH_TTL:
            return False
        if _recently_attempted_on_disk():
            _LAST_ATTEMPT = now  # memoise so we don't stat the disk every call
            return False
        _LAST_ATTEMPT = now
        _IN_FLIGHT = True
    _mark_attempt_on_disk()  # throttles a failed fetch across processes too
    return True


def _end_attempt() -> None:
    global _IN_FLIGHT  # pylint: disable=global-statement
    with _lock:
        _IN_FLIGHT = False


def _attempt_path() -> str:
    return seed_index_cache_path() + ".attempt"


def _recently_attempted_on_disk() -> bool:
    try:
        return (time.time() - os.path.getmtime(_attempt_path())) < _REFRESH_TTL
    except OSError:
        return False


def _mark_attempt_on_disk() -> None:
    path = _attempt_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError:
        pass
