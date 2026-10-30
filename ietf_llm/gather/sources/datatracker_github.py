"""Resolve GitHub logins via Datatracker self-reported profile resources.

Datatracker lets each person attach external resources to their
profile; one resource type is `github_username`. That yields an
*authoritative* login → person mapping — the person set it themselves —
which we can join to the mailing-list registry by the person's
**verified email addresses**. That is an exact-key link, unlike the
display-name heuristics elsewhere in `people.py`, so it carries no
two-different-people-same-name collision risk.

Coverage is partial: only a few hundred people across all of IETF have
set a `github_username`. But where present it is high-precision, so the
registry tries this *before* falling back to the fuzzier
GitHub-display-name merge.

Cache (`~/.cache/ietf-llm/_datatracker-github.json`, shared across WGs
because the mapping is WG-independent):

  - `"_index"`: `{ "<login-lowercased>": "<person resource_uri>" }` —
    the global `github_username → person` map, walked (paginated) and
    reused until it is older than `identity_cache.INDEX_TTL_SECONDS` (7
    days), when it is rebuilt (people add the resource after their first
    gather). A login
    absent from the index means "nobody on Datatracker claims it"; we
    record that as a `None` per-login entry. A `None` is only a note: it is
    checked against the current index each run (a dict lookup, no request),
    so it never outlives a rebuild that finds the login.
  - per-login: `{ "name": ..., "emails": [...], "fetched_at": ... }` —
    the resolved real name and verified emails for a login we actually
    needed, looked up again once older than `identity_cache.TTL_SECONDS`
    (30 days; at most `_MAX_REFRESH` entries, so 2x that many requests, per
    call, so a cache that all aged out together does not become one burst).
    A refresh that fails keeps the entry and adds `"checked_at"`; the entry
    is not retried until that is itself a TTL old, so a deleted or merged
    person does not cost a request and a slot every run. `null` when the
    login isn't in the index.

Best-effort throughout: a network error or unexpected response skips
the login (returns nothing for it) and leaves the cache untouched so
the next gather retries. Datatracker is not aggressively rate-limited
and needs no token, so there is no 403 back-off path like the GitHub
users client has.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...log import LogLevel, Verbosity, log
from ...paths import get_cache_dir
from . import identity_cache
from .datatracker import _get_json

_CACHE_FILENAME = "_datatracker-github.json"
_INDEX_KEY = "_index"
_INDEX_STAMP_KEY = "_index_fetched_at"
_EXTRESOURCE_PATH = "/api/v1/person/personextresource/?name=github_username&limit=100"
_PERSON_ID_RE = re.compile(r"/person/(\d+)/?$")
#: Expired per-login entries refreshed per call (two requests each); the rest
#: keep their old value until a later call. Not a process-wide budget: the
#: runner gathers corpora concurrently, each making its own call.
_MAX_REFRESH = 50


@dataclass
class DTResolved:
    """Per-login resolution from Datatracker: the person's real name and
    every email address Datatracker has on file for them (active or not —
    an old address is exactly what links a long-time list participant)."""

    name: Optional[str] = None
    emails: List[str] = field(default_factory=list)


# --- Public entry point ---------------------------------------------------


def resolve_via_datatracker(
    logins: List[str],
    verbose: Verbosity = Verbosity.STATUS,
) -> Dict[str, DTResolved]:
    """Resolve GitHub `logins` to `{login: DTResolved}` via Datatracker.

    Only logins that someone has claimed as their `github_username` on a
    Datatracker profile resolve to a non-empty result; the rest are
    omitted from the returned dict (the caller keeps them for the
    GitHub-API name fallback). Matching is case-insensitive.

    Strategic about request volume: the global `github_username` index is
    built once and cached; per-login name/email lookups are cached too.
    """
    wanted = [login.strip() for login in logins if login and login.strip()]
    if not wanted:
        return {}

    cache = _load_cache()
    index = _ensure_index(cache, verbose)
    if index is None:
        # Index build failed (network); nothing we can do this run.
        return {}

    out: Dict[str, DTResolved] = {}
    n_requested = 0
    n_refreshed = 0
    dirty = False
    for login in wanted:
        key = login.lower()
        entry = cache.get(key)
        refreshing = False
        if isinstance(entry, dict):
            out[login] = DTResolved(
                name=entry.get("name"),
                emails=list(entry.get("emails") or []),
            )
            if not identity_cache.is_expired(entry.get("fetched_at")):
                continue
            if not identity_cache.is_expired(entry.get("checked_at")):
                continue  # a recent refresh failed; back off
            refreshing = True
        person_uri = index.get(key)
        if not person_uri:
            if key not in cache:
                # Not claimed by anyone on Datatracker. Note it; the index
                # is consulted again next run, so this is not a decision.
                cache[key] = None
                dirty = True
            # A resolved login that has since left the index keeps its entry.
            continue
        if refreshing:
            # Spent only on an entry that makes a request.
            if n_refreshed >= _MAX_REFRESH:
                continue
            n_refreshed += 1
        resolved = _resolve_one(person_uri)
        n_requested += 1
        if resolved is None:
            if refreshing and isinstance(entry, dict):
                # Keep the old value, but don't retry (and spend a slot) next run.
                entry["checked_at"] = identity_cache.now_iso()
                dirty = True
            # A first lookup that fails is not cached: retried next run.
            continue
        if (
            refreshing
            and isinstance(entry, dict)
            and not (resolved.name or resolved.emails)
            and (entry.get("name") or entry.get("emails"))
        ):
            # An empty answer is not evidence that the person lost their
            # details: keep what we had and restart its clock.
            entry["fetched_at"] = identity_cache.now_iso()
            dirty = True
            continue
        cache[key] = {
            "name": resolved.name,
            "emails": resolved.emails,
            "fetched_at": identity_cache.now_iso(),
        }
        dirty = True
        if resolved.name or resolved.emails:
            out[login] = resolved

    if dirty:
        _merge_save(cache)
    if n_requested:
        log(
            f"Datatracker github_username lookups: {n_requested} resolved",
            verbose,
            level=LogLevel.STATUS,
        )
    return out


# --- Index ----------------------------------------------------------------


def _ensure_index(
    cache: Dict[str, Any], verbose: Verbosity
) -> Optional[Dict[str, str]]:
    """Return the cached `github_username → person_uri` index, building it
    (one paginated walk of the personextresource endpoint) if absent or older
    than `INDEX_TTL_SECONDS`.

    A stale index is kept when the rebuild fails, in full or part way: it is
    still better than none, or than a truncated one. Returns None only when
    there is no index at all AND the build fails, so the caller can abort
    cleanly. A successfully built index is written back into `cache` under
    `_INDEX_KEY`."""
    existing = cache.get(_INDEX_KEY)
    if isinstance(existing, dict):
        if not identity_cache.is_expired(
            cache.get(_INDEX_STAMP_KEY), identity_cache.INDEX_TTL_SECONDS
        ):
            return existing
    else:
        existing = None

    index: Dict[str, str] = {}
    path: Optional[str] = _EXTRESOURCE_PATH
    pages = 0
    while path:
        body = _get_json(path)
        if not body:
            # First page failing means no index at all; bail. A later
            # page failing leaves a partial index, which is still useful.
            if existing is not None:
                return existing
            if pages == 0:
                return None
            break
        for obj in body.get("objects") or []:
            value = obj.get("value")
            person = obj.get("person")
            if isinstance(value, str) and isinstance(person, str) and value.strip():
                index[value.strip().lower()] = person
        pages += 1
        path = (body.get("meta") or {}).get("next") or None

    if existing is not None and len(index) * 2 < len(existing):
        # An empty or near-empty walk of a healthy index is a bad response,
        # not a mass un-claiming; keep the old one and try again next run.
        return existing
    cache[_INDEX_KEY] = index
    cache[_INDEX_STAMP_KEY] = identity_cache.now_iso()
    _merge_save(cache)
    log(
        f"Built Datatracker github_username index: {len(index)} logins",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return index


def _resolve_one(person_uri: str) -> Optional[DTResolved]:
    """Fetch a person's real name and email addresses. Returns None on a
    transient failure (so the caller retries next run)."""
    match = _PERSON_ID_RE.search(person_uri)
    if not match:
        return None
    person_id = match.group(1)

    person_body = _get_json(person_uri)
    if person_body is None:
        return None
    name = person_body.get("name") or person_body.get("ascii") or None
    cleaned_name = name.strip() if isinstance(name, str) else None

    emails_body = _get_json(f"/api/v1/person/email/?person={person_id}&limit=100")
    if emails_body is None:
        return None
    emails: List[str] = []
    for obj in emails_body.get("objects") or []:
        addr = obj.get("address")
        if isinstance(addr, str) and addr.strip():
            emails.append(addr.strip())

    return DTResolved(name=cleaned_name or None, emails=emails)


# --- Cache file -----------------------------------------------------------


def _cache_path() -> str:
    return os.path.join(get_cache_dir(), _CACHE_FILENAME)


def cache_path() -> str:
    """Public alias of the cache-file path, for the cloud gather-cache sync
    (`gather.cache_sync`), which round-trips this shared identity map to durable
    storage across a scale-to-zero wipe (issue #82)."""
    return _cache_path()


def merge_cache(remote: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two caches losslessly. The map holds the global `github_username`
    index (`_index` + `_index_fetched_at`) plus per-login entries (`{name,
    emails, fetched_at}`, or `None` for "confirmed not on Datatracker").

    Keep the index with the newer stamp (it is rebuilt wholesale, so last-built
    wins rather than merged). The per-login entries fold in via the shared
    stamp-wins union: on a login both hold, the newer `fetched_at` wins, so a
    real resolution beats a bare `None` miss."""
    merged: Dict[str, Any] = {}
    if str(remote.get(_INDEX_STAMP_KEY, "")) >= str(local.get(_INDEX_STAMP_KEY, "")):
        index_src = remote
    else:
        index_src = local
    if _INDEX_KEY in index_src:
        merged[_INDEX_KEY] = index_src[_INDEX_KEY]
    if _INDEX_STAMP_KEY in index_src:
        merged[_INDEX_STAMP_KEY] = index_src[_INDEX_STAMP_KEY]
    return identity_cache.union_newer_by_stamp(
        remote, local, into=merged, skip=frozenset({_INDEX_KEY, _INDEX_STAMP_KEY})
    )


def _load_cache() -> Dict[str, Any]:
    return identity_cache.load(_cache_path())


def _save_cache(cache: Dict[str, Any]) -> None:
    identity_cache.save(_cache_path(), cache)


#: Serialises the reload-merge-save so the runner's concurrent same-process
#: gathers don't clobber each other's local additions (index or per-login).
_CACHE_LOCK = threading.Lock()


def _merge_save(cache: Dict[str, Any]) -> None:
    identity_cache.merge_save(
        _CACHE_LOCK, _cache_path(), _load_cache, merge_cache, cache
    )
