"""Local mirror of the seed store's index (issue #182), so `list_corpora` can
show what is available to fast-start.

`cached_index` is a plain **offline** read of a local mirror. `refresh_mirror`
keeps that mirror current with a live fetch — but only under the gather gate
(`gather_enabled`), the project's sanctioned networked-read exception (like the
live Datatracker lookups): a read-only HTTP replica never fetches, while a local
stdio server (where a cold-start client has nothing gathered yet) can. The fetch
is bounded (short timeout) and throttled (at most once per `_REFRESH_TTL`, even on
failure), so `list_corpora` stays fast and never hangs on a slow mirror.

Module-level imports stay read-safe (stdlib + `seed.format` + `paths`); the
network-touching `seed.fetch`, `config.service`, and `freshness` are imported
lazily inside `refresh_mirror`, so a plain `cached_index` read pulls nothing.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..paths import seed_index_cache_path
from . import format as fmt

#: `list_corpora` refreshes the mirror at most this often (seconds), so a burst of
#: calls does one fetch and a down mirror is retried at most hourly.
_REFRESH_TTL = 3600.0

#: Bound on the live index fetch, short so a read tool never blocks on a slow host.
_FETCH_TIMEOUT = 5.0


def refresh_mirror() -> None:
    """Refresh the local mirror with a live fetch when it is stale, gated and
    throttled (see the module docstring). Best-effort and silent: no-op when
    gather is disabled or no seed URL is set, and a failed fetch leaves the last
    good mirror in place."""
    # pylint: disable=import-outside-toplevel
    from ..config import service
    from ..freshness import gather_enabled

    if not gather_enabled() or not service.seeding_enabled():
        return
    seed_url = service.seed_url()
    if not seed_url or _recently_attempted():
        return
    _mark_attempt()  # before the fetch, so a failure is throttled too
    from . import fetch as seed_fetch

    index = seed_fetch.load_index(seed_url, timeout=_FETCH_TIMEOUT)
    if index is not None:
        cache_index(index)


def _attempt_path() -> str:
    return seed_index_cache_path() + ".attempt"


def _recently_attempted() -> bool:
    try:
        return (time.time() - os.path.getmtime(_attempt_path())) < _REFRESH_TTL
    except OSError:
        return False


def _mark_attempt() -> None:
    path = _attempt_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
    except OSError:
        pass


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
