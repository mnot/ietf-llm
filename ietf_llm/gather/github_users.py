"""Resolve GitHub usernames to real names via the public users API.

Why: the existing Registry heuristic links a GitHub login to a Person
when an already-known email's local-part matches the login (e.g.
`mnot@mnot.net` ↔ `mnot`). For GitHub-only contributors — people whose
only corpus presence is GitHub issues, with no matching mailing-list
identity — that heuristic produces nothing, and the Person's
`canonical_name` ends up being the bare login. A consuming LLM then
sees `kmadhavan-msft` or `TimidRobot` in search hits / digests and
can't tell who that actually is.

This module fills that gap. For each unresolved login, GET
`/users/<login>` returns a `name` field (when the user has set one);
we update the Person's canonical_name to the real name.

Deliberate non-features:

- **No company / affiliation lookup.** GitHub's `company` field is
  available but the SKILL.md norms (which we surfaced earlier) say
  affiliation does not equal authority in the IETF. Using `company`
  would invite consumers to read "Microsoft says X" when really X is
  a person speaking as an individual. Names only.

- **No retry on rate limit.** The GitHub API returns 403 once the
  hourly window is exhausted. We back off, finish this gather with
  what we have, and pick up the rest on the next run (cache means
  the work isn't repeated).

- **Cache hits cached too.** A login that returns 404 is cached as
  `{"name": null, "fetched_at": …}` so we don't waste a request on
  it again. Renamed / deleted users stay missing until the cache
  file is hand-deleted.

Cache lives at `~/.cache/ietf-llm/_github-users.json` — shared across
WGs because the same Person appears across many groups, and the API
result is WG-independent.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from ..utils import (
    DEFAULT_HEADERS,
    LogLevel,
    Verbosity,
    get_cache_dir,
    governed_get,
    log,
)
from . import identity_cache

_CACHE_FILENAME = "_github-users.json"
_USER_API = "https://api.github.com/users/"


@dataclass
class _Outcome:
    """Per-call result envelope. Distinguishes 'we got a real name'
    from 'we know there is no name' from 'we couldn't ask right now'."""

    name: Optional[str]
    # True when we should write the result to the cache (resolved name
    # OR confirmed missing). False for transient failures (rate limit,
    # network) so we retry next time.
    cacheable: bool
    # True if this call hit a 403 rate limit. The caller should stop
    # making further requests this run.
    rate_limited: bool = False
    # User's self-reported `company` field (free text, often blank).
    # Captured for affiliation signal: corroborates draft-derived
    # affiliation when both agree; stands as a weaker independent
    # signal when no draft authorship exists for this person.
    company: Optional[str] = None


# --- Public entry point ---------------------------------------------------


@dataclass
class _Resolved:
    """Per-login resolution result returned to the caller."""

    name: Optional[str]
    company: Optional[str] = None


def resolve_logins(
    logins: list[str],
    verbose: Verbosity = Verbosity.STATUS,
) -> Dict[str, _Resolved]:
    """Resolve a batch of GitHub logins to real names + company strings.

    Returns `{login: _Resolved(name, company)}`. Either field can be
    None when GitHub returned no value (cleared by the user, or the
    user doesn't exist). Logins missing from the result are ones we
    couldn't ask about right now (rate limit / network error); the
    caller should keep their existing canonical name unchanged for
    those.

    Strategic about request volume: anything in the on-disk cache is
    returned from there. Only un-cached logins hit the network. Once
    a request hits 403, we stop making more this run.

    Old cache entries (pre-company) lack `company`; treated as None
    on read — they'll get refreshed on the next gather when the
    entry expires (we don't currently expire entries, so practically:
    on cache file deletion).
    """
    cache = _load_cache()
    out: Dict[str, _Resolved] = {}
    headers = _build_headers()
    n_requested = 0
    rate_limited = False
    for login in logins:
        if not login:
            continue
        if login in cache:
            entry = cache[login]
            out[login] = _Resolved(
                name=entry.get("name"),
                company=entry.get("company"),
            )
            continue
        if rate_limited:
            # Don't bother trying; the response would just be another
            # 403 and we'd burn against the rate limit log.
            continue
        outcome = _fetch_one(login, headers, verbose)
        n_requested += 1
        if outcome.rate_limited:
            rate_limited = True
        if outcome.cacheable:
            cache[login] = {
                "name": outcome.name,
                "company": outcome.company,
                "fetched_at": _now_iso(),
            }
        if outcome.name is not None or outcome.cacheable:
            out[login] = _Resolved(
                name=outcome.name,
                company=outcome.company,
            )
    if n_requested:
        # Persist whatever we learned. Merge under a lock against the current
        # on-disk file so a concurrent same-process gather's additions are not
        # clobbered (load-modify-save would race; issue #82 review). Best-effort:
        # a write failure just means next run repeats the requests.
        _merge_save(cache)
        log(
            f"GitHub user lookups: {n_requested} requested, "
            f"{len(cache)} now cached" + (" (rate limited)" if rate_limited else ""),
            verbose,
            level=LogLevel.STATUS,
        )
    return out


# --- Internals ------------------------------------------------------------


def _build_headers() -> Dict[str, str]:
    """Authenticated when GITHUB_TOKEN is set. Same env var the issue
    fetcher already uses; one token covers both surfaces."""
    headers = {
        **DEFAULT_HEADERS,
        "Accept": "application/vnd.github.v3+json",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _fetch_one(  # pylint: disable=too-many-return-statements
    login: str,
    headers: Dict[str, str],
    verbose: Verbosity,
) -> _Outcome:
    """One `/users/<login>` request. Maps response shapes to outcomes
    rather than raising — see _Outcome docstring."""
    url = _USER_API + login
    try:
        response = governed_get(url, headers=headers, timeout=10.0)
    except requests.RequestException as err:
        log(
            f"GitHub user lookup transient error for {login}: "
            f"{type(err).__name__}: {err}",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return _Outcome(name=None, cacheable=False)
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return _Outcome(name=None, cacheable=False)
        if not isinstance(body, dict):
            return _Outcome(name=None, cacheable=False)
        name = body.get("name")
        company = body.get("company")
        # Empty-string fields happen — GitHub returns "" rather than
        # null when the user clears them. Normalise to None.
        cleaned_name = str(name).strip() if isinstance(name, str) else None
        cleaned_company = str(company).strip() if isinstance(company, str) else None
        return _Outcome(
            name=cleaned_name or None,
            cacheable=True,
            company=cleaned_company or None,
        )
    if response.status_code == 404:
        # User doesn't exist (renamed, deleted, or typo). Cache the
        # miss so we don't re-ask.
        return _Outcome(name=None, cacheable=True)
    if response.status_code in (403, 429):
        # Rate limited. Stop asking this run.
        log(
            f"GitHub API rate limited at user lookup for {login}; "
            "remaining lookups skipped this run.",
            verbose,
            level=LogLevel.STATUS,
        )
        return _Outcome(name=None, cacheable=False, rate_limited=True)
    # 5xx and the long tail of weird responses. Treat as transient.
    log(
        f"GitHub user lookup unexpected status {response.status_code} " f"for {login}.",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return _Outcome(name=None, cacheable=False)


# --- Cache file -----------------------------------------------------------


def _cache_path() -> str:
    """Cache lives in the top-level cache dir, NOT under any single
    WG, because the same user appears across many WGs and the lookup
    result is identical regardless."""
    return os.path.join(get_cache_dir(), _CACHE_FILENAME)


def cache_path() -> str:
    """Public alias of the cache-file path, for the cloud gather-cache sync
    (`gather.cache_sync`), which round-trips this shared identity map to durable
    storage across a scale-to-zero wipe (issue #82)."""
    return _cache_path()


def merge_cache(remote: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """Merge two `login -> {name, company, fetched_at}` maps losslessly.

    The map is append-mostly and corpus-independent, so a concurrent fleet
    gather may have added logins to `remote` that this gather's `local` lacks,
    and vice versa. Union both; on a login both hold, keep the newer `fetched_at`
    (a later lookup may have filled in a previously-null name). Entries are only
    kept when they are dicts, mirroring `_load_cache`'s validation."""
    merged: Dict[str, Any] = {
        login: entry for login, entry in remote.items() if isinstance(entry, dict)
    }
    for login, entry in local.items():
        if not isinstance(entry, dict):
            continue
        current = merged.get(login)
        if current is None or str(entry.get("fetched_at", "")) >= str(
            current.get("fetched_at", "")
        ):
            merged[login] = entry
    return merged


def _load_cache() -> Dict[str, Dict[str, Any]]:
    """Parse the cache file. Returns empty dict on any read / parse
    error — we'd rather refetch than block on stale state."""
    path = _cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {login: entry for login, entry in data.items() if isinstance(entry, dict)}


def _save_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    identity_cache.save(_cache_path(), cache)


#: Serialises the reload-merge-save so the runner's concurrent same-process
#: gathers don't clobber each other's local additions.
_CACHE_LOCK = threading.Lock()


def _merge_save(cache: Dict[str, Dict[str, Any]]) -> None:
    identity_cache.merge_save(
        _CACHE_LOCK, _cache_path(), _load_cache, merge_cache, cache
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
