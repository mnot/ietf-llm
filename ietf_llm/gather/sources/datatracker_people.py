"""Resolve mailing-list addresses to a Datatracker person id, the identity
spine for cross-address mail-side dedup.

Datatracker records every email address a person has ever registered —
active and historical — under one person id. That lets us recognise the
same human posting under unrelated addresses with different name
spellings (`M. Nottingham <mnot@fastly.com>` vs `Mark Nottingham
<mnot@mnot.net>`), which the name-string / DMARC merge in `people.py`
misses. The join is by exact email against a curated table, so it carries
none of the two-different-people-same-name risk of name matching.

This is the mail-side counterpart to `datatracker_github.py` (which
attaches GitHub logins to a person): here the person id consolidates the
mail identities themselves. Both join through the same Datatracker person.

Query shape: the email endpoint batch-filters with `address__in`, and each
returned row already carries its `person` uri — so a chunk of ~50
addresses resolves to one request with no per-person follow-up (a handful
of calls per WG, not one per participant).

Cache (`~/.cache/ietf-llm/_datatracker-people.json`, shared across WGs
because the mapping is WG-independent), per normalised address:

  - `{ "person": "<person resource_uri>", "fetched_at": ... }` — a hit.
  - `null` — confirmed: Datatracker has no record of this address, so we
    don't re-query it every gather.

Best-effort: a network error (None for a whole chunk) is not cached, so
the next gather retries. Datatracker is not aggressively rate-limited and
needs no token, so there is no 403 back-off path.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ...log import LogLevel, Verbosity, log
from ...paths import get_cache_dir
from . import identity_cache
from .datatracker import _get_json

_CACHE_FILENAME = "_datatracker-people.json"
#: Addresses per `address__in` request. Datatracker returns one row per
#: address, so a chunk this size stays within the `limit=100` single page.
_CHUNK = 50


# --- Public entry point ---------------------------------------------------


def resolve_addresses(
    addresses: List[str],
    verbose: Verbosity = Verbosity.STATUS,
) -> Dict[str, str]:
    """Resolve mail `addresses` to `{address: person_uri}` via Datatracker.

    Addresses must already be normalised (lower-cased, DMARC de-munged,
    relay addresses dropped) — that is how they are stored in the registry.
    Only addresses Datatracker has a record for appear in the result; the
    rest are omitted (and cached as confirmed misses). Per-address hits and
    misses are cached, so a second pass over the same addresses makes no
    further requests.
    """
    wanted = [a.strip().lower() for a in addresses if a and a.strip()]
    if not wanted:
        return {}

    cache = _load_cache()
    out: Dict[str, str] = {}
    to_query: List[str] = []
    for addr in wanted:
        if addr in cache:
            entry = cache[addr]
            if isinstance(entry, dict):
                uri = entry.get("person")
                if isinstance(uri, str):
                    out[addr] = uri
            # `None` entry means "confirmed not on Datatracker"; skip.
            continue
        to_query.append(addr)

    dirty = False
    n_resolved = 0
    for start in range(0, len(to_query), _CHUNK):
        chunk = to_query[start : start + _CHUNK]
        resolved = _resolve_chunk(chunk)
        if resolved is None:
            # Transient failure for the whole chunk — don't cache, retry next
            # run.
            continue
        for addr in chunk:
            uri = resolved.get(addr)
            if uri:
                cache[addr] = {"person": uri, "fetched_at": identity_cache.now_iso()}
                out[addr] = uri
                n_resolved += 1
            else:
                # In the response but unmatched: Datatracker has no record.
                cache[addr] = None
            dirty = True

    if dirty:
        _merge_save(cache)
    if n_resolved:
        log(
            f"Datatracker address lookups: {n_resolved} resolved to a person",
            verbose,
            level=LogLevel.STATUS,
        )
    return out


def _resolve_chunk(addresses: List[str]) -> Optional[Dict[str, str]]:
    """Resolve one chunk of addresses to `{address: person_uri}`.

    Returns None on a transient failure (so the caller leaves the chunk
    uncached and retries next run). An empty dict means the request
    succeeded but Datatracker matched none of them.
    """
    joined = ",".join(quote(addr, safe="") for addr in addresses)
    body = _get_json(f"/api/v1/person/email/?address__in={joined}&limit=100")
    if body is None:
        return None
    out: Dict[str, str] = {}
    for obj in body.get("objects") or []:
        addr = obj.get("address")
        person = obj.get("person")
        if isinstance(addr, str) and isinstance(person, str) and addr.strip():
            out[addr.strip().lower()] = person
    return out


# --- Cache file -----------------------------------------------------------


def _cache_path() -> str:
    return os.path.join(get_cache_dir(), _CACHE_FILENAME)


def cache_path() -> str:
    """Public alias of the cache-file path, for the cloud gather-cache sync
    (`gather.cache_sync`), which round-trips this shared identity map to durable
    storage across a scale-to-zero wipe (issue #82)."""
    return _cache_path()


def merge_cache(remote: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two caches losslessly. Each holds per-address entries (`{person,
    fetched_at}` for a hit, or `None` for "confirmed not on Datatracker"). The
    shared stamp-wins union keeps the newer `fetched_at` on a shared address, so
    a real resolution beats a bare `None` miss."""
    return identity_cache.union_newer_by_stamp(remote, local)


def _load_cache() -> Dict[str, Any]:
    return identity_cache.load(_cache_path())


def _save_cache(cache: Dict[str, Any]) -> None:
    identity_cache.save(_cache_path(), cache)


#: Serialises the reload-merge-save so the runner's concurrent same-process
#: gathers don't clobber each other's local additions.
_CACHE_LOCK = threading.Lock()


def _merge_save(cache: Dict[str, Any]) -> None:
    identity_cache.merge_save(
        _CACHE_LOCK, _cache_path(), _load_cache, merge_cache, cache
    )
