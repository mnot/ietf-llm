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
    the global `github_username → person` map, walked once (paginated)
    and reused. A login absent from the index means "nobody on
    Datatracker claims it"; we record that as a `None` per-login entry
    so we don't rebuild the index hunting for it.
  - per-login: `{ "name": ..., "emails": [...], "fetched_at": ... }` —
    the resolved real name and verified emails for a login we actually
    needed. `null` when the login isn't in the index.

Best-effort throughout: a network error or unexpected response skips
the login (returns nothing for it) and leaves the cache untouched so
the next gather retries. Datatracker is not aggressively rate-limited
and needs no token, so there is no 403 back-off path like the GitHub
users client has.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..utils import LogLevel, Verbosity, get_cache_dir, log
from .datatracker import _get_json

_CACHE_FILENAME = "_datatracker-github.json"
_INDEX_KEY = "_index"
_INDEX_STAMP_KEY = "_index_fetched_at"
_EXTRESOURCE_PATH = "/api/v1/person/personextresource/?name=github_username&limit=100"
_PERSON_ID_RE = re.compile(r"/person/(\d+)/?$")


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
    dirty = False
    for login in wanted:
        key = login.lower()
        if key in cache:
            entry = cache[key]
            if isinstance(entry, dict):
                out[login] = DTResolved(
                    name=entry.get("name"),
                    emails=list(entry.get("emails") or []),
                )
            # `None` entry means "confirmed not on Datatracker"; skip.
            continue
        person_uri = index.get(key)
        if not person_uri:
            # Not claimed by anyone on Datatracker. Record the miss so we
            # don't re-check every gather.
            cache[key] = None
            dirty = True
            continue
        resolved = _resolve_one(person_uri)
        n_requested += 1
        if resolved is None:
            # Transient failure — don't cache, retry next run.
            continue
        cache[key] = {
            "name": resolved.name,
            "emails": resolved.emails,
            "fetched_at": _now_iso(),
        }
        dirty = True
        if resolved.name or resolved.emails:
            out[login] = resolved

    if dirty:
        _save_cache(cache)
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
    (one paginated walk of the personextresource endpoint) if absent.

    Returns None only when the index is absent AND the build fails, so the
    caller can abort cleanly. A successfully built index is written back
    into `cache` under `_INDEX_KEY`."""
    existing = cache.get(_INDEX_KEY)
    if isinstance(existing, dict):
        return existing

    index: Dict[str, str] = {}
    path: Optional[str] = _EXTRESOURCE_PATH
    pages = 0
    while path:
        body = _get_json(path)
        if not body:
            # First page failing means no index at all; bail. A later
            # page failing leaves a partial index, which is still useful.
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

    cache[_INDEX_KEY] = index
    cache[_INDEX_STAMP_KEY] = _now_iso()
    _save_cache(cache)
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
    cleaned_name = str(name).strip() if isinstance(name, str) else None

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


def _load_cache() -> Dict[str, Any]:
    path = _cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: Dict[str, Any]) -> None:
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
