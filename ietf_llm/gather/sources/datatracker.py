"""Thin client for the IETF Datatracker JSON API.

We only use the small slice we need: chairs / ADs / advisors / etc.
for a given WG. The Datatracker exposes other endpoints (drafts,
documents, ballots, …) which could be added here later in the same
shape.

Why a real API client and not HTML scraping the WG about page:
the about page's table layout has changed before; the JSON API has
been stable for years and returns canonical IDs we can correlate
with the Registry.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from ...net import http_metrics
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get, http_error_detail
from ...paths import get_cache_dir
from .http_cache import HttpCache

_API_BASE = "https://datatracker.ietf.org/api/v1"

# Map Datatracker role slugs to a friendly display label. Anything not
# in this map is included verbatim with a leading uppercase letter.
_ROLE_LABELS = {
    "chair": "Chair",
    "ad": "Area Director",
    "techadv": "Tech Advisor",
    "secr": "Secretary",
    "auth": "Author",
    "editor": "Editor",
    "delegate": "Delegate",
}


@dataclass
class Role:
    """One role assignment for one person on a WG."""

    role: str  # raw Datatracker slug (e.g. "chair", "ad")
    label: str  # human label (e.g. "Chair", "Area Director")
    name: str  # full display name from Datatracker
    email: Optional[str]  # mailbox if extractable


def label_for(role_slug: str) -> str:
    """Human-readable label for a Datatracker role slug."""
    return _ROLE_LABELS.get(role_slug, role_slug.capitalize())


# --- API client ------------------------------------------------------------


_EMAIL_FROM_URL = re.compile(r"/api/v1/person/email/([^/]+)/?$")
_ROLE_NAME_FROM_URL = re.compile(r"/api/v1/name/rolename/([^/]+)/?$")
_PERSON_ID_FROM_URL = re.compile(r"/person/person/(\d+)/?")


# --- Conditional-GET cache -------------------------------------------------
#
# The Datatracker JSON API returns ETags and honours If-None-Match (304); the
# store that persists them (`http_cache.HttpCache`) lets re-gathers revalidate
# cheaply instead of re-downloading. It is machinery: one shared directory at
# ~/.cache/ietf-llm/.http-cache/_shared/, or one per corpus on the cloud backend,
# loaded lazily and flushed atomically.


#: The process-default ETag store: corpus-agnostic, on persistent disk at the
#: cache root. Used by the local CLI and any gather that has not bound a corpus.
#: Lazy so get_cache_dir() is read on first use, not at import.
_DEFAULT_CACHE: Optional[HttpCache] = None
_default_lock = threading.Lock()

#: The active per-corpus ETag store for *this thread*. A gather runs on one
#: thread, and the runner's worker threads are reused, so the binding is
#: per thread and scoped to one gather (`gather_http_cache`). Unset means "use
#: the default store".
_active = threading.local()


def _default_http_cache() -> HttpCache:
    global _DEFAULT_CACHE  # pylint: disable=global-statement
    with _default_lock:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = HttpCache(
                http_cache_path(), legacy=legacy_http_cache_path()
            )
        return _DEFAULT_CACHE


def _active_http_cache() -> HttpCache:
    """This thread's ETag store: a bound per-corpus store if one is set (a
    concurrent gather), else the process-default store."""
    cache = getattr(_active, "cache", None)
    return cache if cache is not None else _default_http_cache()


def http_cache_path(corpus: Optional[str] = None) -> str:
    """Local directory of the conditional-GET/ETag store for `corpus` (or the
    process-default store when None). Public so the cloud gather-cache sync
    (`gather.cache_sync`) can round-trip it to durable storage across a
    scale-to-zero wipe (issue #82). It lives *outside* any corpus workspace (a
    dot-prefixed sibling dir), so it is never published as version content nor
    mistaken for a corpus by the cache lister. Corpus names start with an
    alphanumeric, so `_shared` cannot collide with one."""
    return os.path.join(get_cache_dir(), ".http-cache", corpus or "_shared")


def legacy_http_cache_path(corpus: Optional[str] = None) -> str:
    """Where earlier versions kept the store: a single JSON file holding every
    body. `HttpCache` migrates one it finds there."""
    if corpus:
        return os.path.join(get_cache_dir(), ".http-cache", f"{corpus}.json")
    return os.path.join(get_cache_dir(), ".http-cache.json")


def bind_gather_corpus(corpus: Optional[str]) -> None:
    """Bind this thread's ETag store to `corpus`'s own directory -- a *fresh*
    instance, so it reloads the index on disk rather than carrying stale in-memory
    entries from a prior gather on this reused worker thread. Pass None to unbind
    (back to the process-default store) and release the per-corpus one."""
    _active.cache = (
        HttpCache(
            http_cache_path(corpus),
            persist_at_exit=False,
            legacy=legacy_http_cache_path(corpus),
        )
        if corpus
        else None
    )


def flush_http_cache() -> None:
    """Force this thread's active ETag store to disk now. A per-corpus store
    never flushes at exit, so `gather_http_cache` calls this when its gather ends."""
    _active_http_cache().flush()


@contextmanager
def gather_http_cache(corpus: Optional[str]) -> Iterator[None]:
    """Run one gather against `corpus`'s own ETag store, then flush it.

    The cloud gather-cache sync restores `http_cache_path(corpus)` before the
    gather and uploads it after, in a different process when the pipeline runs in
    the gather child. The directory is the only thing they share, so the binding
    belongs here, around the gather itself, in whichever process runs it. `None`
    leaves the process-default store alone: the local backend keeps one shared
    store on persistent disk and syncs nothing."""
    if not corpus:
        yield
        return
    bind_gather_corpus(corpus)
    try:
        yield
    finally:
        try:
            flush_http_cache()
        finally:
            bind_gather_corpus(None)


def _get_json(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """GET a Datatracker JSON endpoint, revalidating via ETag.

    Returns the decoded body or None. On a 304 (or a network error with a
    cached entry) the previously-stored body is reused, so re-gathers
    transfer almost nothing for unchanged endpoints.
    """
    url = (
        path_or_url
        if path_or_url.startswith("http")
        else f"https://datatracker.ietf.org{path_or_url}"
    )
    url = url + ("&format=json" if "?" in url else "?format=json")

    cache = _active_http_cache()
    entry = cache.get(url)

    def cached() -> Optional[Dict[str, Any]]:
        return _decode_cached(cache.body(url)) if entry else None

    headers = dict(DEFAULT_HEADERS)
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]

    try:
        response = governed_get(url, headers=headers, timeout=timeout)
    except requests.RequestException as err:
        http_metrics.record(url, 0, 0, error=True)
        log(
            f"Datatracker: fetch {url} failed: {http_error_detail(err)}",
            level=LogLevel.WARN,
        )
        return cached()

    if response.status_code == 304:
        http_metrics.record(url, 304, len(response.content))
        return cached()
    try:
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as err:
        http_metrics.record(
            url, response.status_code, len(response.content), error=True
        )
        log(
            f"Datatracker: fetch {url} failed: {http_error_detail(err)}",
            level=LogLevel.WARN,
        )
        return cached()
    http_metrics.record(url, response.status_code, len(response.content))
    if not isinstance(result, dict):
        return None
    etag = response.headers.get("ETag")
    if etag:
        cache.store(url, etag, response.text)
    return result


def _decode_cached(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a cached response body to a dict, or None."""
    try:
        decoded = json.loads(body or "")
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def iter_group_documents(wg_name: str, doc_type: str) -> Iterator[Dict[str, Any]]:
    """Yield every Datatracker document object of `doc_type` (`draft`,
    `rfc`, `polls`, …) whose responsible group is `wg_name`.

    Pages through `meta.next` so a group with hundreds of documents is
    never silently truncated. Empty if the API is unreachable or the
    group has no documents of that type. Canonical document lister for
    the gather layer (drafts, RFCs, session polls all use it).
    """
    path: Optional[str] = (
        f"{_API_BASE}/doc/document/?group__acronym={wg_name}"
        f"&type={doc_type}&limit=200"
    )
    while path:
        body = _get_json(path)
        if not body:
            return
        yield from body.get("objects") or []
        path = (body.get("meta") or {}).get("next") or None


def draft_state_slugs() -> Dict[str, str]:
    """Map each draft-type Datatracker state's resource URI to its slug.

    The slugs are `active` / `expired` / `rfc` / `repl` (replaced) /
    `auth-rm` / `ietf-rm`. A document object from `iter_group_documents`
    already carries its state resource URIs inline (the `states` list),
    so this one map lets `get_wg_documents` classify every draft's state
    without a per-draft request. There are only a handful of draft
    states, so the single `limit=100` page covers them all.

    Returns `{}` on API failure — callers then record state as None and
    embed the draft (the safe default), so a transient outage never
    drops a draft from the index.
    """
    body = _get_json(f"{_API_BASE}/doc/state/?type__slug=draft&limit=100")
    out: Dict[str, str] = {}
    if not body:
        return out
    for obj in body.get("objects") or []:
        uri = obj.get("resource_uri")
        slug = obj.get("slug")
        if isinstance(uri, str) and isinstance(slug, str):
            out[uri] = slug
    return out


def iter_active_drafts_by_name(wg_name: str) -> Iterator[Dict[str, Any]]:
    """Yield every currently-active Internet-Draft whose name contains
    `-<wg_name>-`.

    Matches Datatracker's "Related Internet-Drafts and RFCs" section on
    the WG documents page: drafts the WG follows but doesn't own, found
    by name pattern rather than group attribution. Caller is responsible
    for the `draft-<author>-<wg>-` position-2 filter — this query alone
    also returns drafts adopted by other WGs whose names happen to
    contain the substring (e.g. `draft-ietf-mailmaint-oauth-public`).
    """
    path: Optional[str] = (
        f"{_API_BASE}/doc/document/?type=draft"
        f"&name__contains=-{wg_name}-"
        f"&states__type__slug=draft&states__slug=active&limit=200"
    )
    while path:
        body = _get_json(path)
        if not body:
            return
        yield from body.get("objects") or []
        path = (body.get("meta") or {}).get("next") or None


def fetch_person_names(person_urls: Iterable[str]) -> Dict[str, str]:
    """Resolve many person resource URLs to display names in one (or a
    few) batched requests.

    Datatracker's Tastypie API honours `id__in=`, so N person
    dereferences collapse to ceil(N/100) list queries instead of N
    individual GETs — the bulk of the per-gather person chatter (WG
    roles, IESG balloters). Returns `{person_url: name}` for every input
    URL whose numeric id the API returned; URLs whose id can't be parsed
    or that the API omitted are simply absent, so callers keep their
    existing per-URL fallback (an email local-part or the raw id).
    """
    # Distinct id → the input URL(s) that referenced it (a person may be
    # named by both path and absolute form across call sites).
    id_to_urls: Dict[str, List[str]] = {}
    for url in person_urls:
        match = _PERSON_ID_FROM_URL.search(url or "")
        if match:
            id_to_urls.setdefault(match.group(1), []).append(url)

    out: Dict[str, str] = {}
    ids = list(id_to_urls)
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        body = _get_json(
            f"{_API_BASE}/person/person/?id__in={','.join(chunk)}&limit=100"
        )
        if not body:
            continue
        for obj in body.get("objects") or []:
            match = _PERSON_ID_FROM_URL.search(obj.get("resource_uri") or "")
            if not match:
                continue
            name = obj.get("name") or obj.get("ascii") or ""
            for url in id_to_urls.get(match.group(1), []):
                out[url] = name
    return out


def fetch_wg_roles(wg: str, verbose: Verbosity = Verbosity.STATUS) -> List[Role]:
    """Return the role assignments for a WG (chairs, ADs, advisors, …).

    Returns an empty list if Datatracker is unreachable or the WG
    isn't recognised. Best-effort: a missing person dereference yields
    a Role with the raw email local-part as the name; we never block
    the gather pipeline on this.
    """
    roles_body = _get_json(f"{_API_BASE}/group/role/?group__acronym={wg}")
    if not roles_body or "objects" not in roles_body:
        log(
            f"Could not fetch Datatracker roles for {wg}; skipping.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    # First parse every role row, then resolve all the people in one
    # batched query (instead of one GET per person).
    parsed: List[Tuple[str, Optional[str], str]] = []  # (slug, email, person_url)
    for obj in roles_body["objects"]:
        role_match = _ROLE_NAME_FROM_URL.search(obj.get("name", ""))
        if not role_match:
            continue
        email_match = _EMAIL_FROM_URL.search(obj.get("email", ""))
        email = email_match.group(1) if email_match else None
        parsed.append((role_match.group(1), email, obj.get("person", "")))

    names = fetch_person_names(person_url for _, _, person_url in parsed)

    out: List[Role] = []
    for role_slug, email, person_url in parsed:
        name = names.get(person_url, "")
        if not name and email:
            name = email.split("@", 1)[0]
        out.append(
            Role(
                role=role_slug,
                label=label_for(role_slug),
                name=name,
                email=email,
            )
        )

    log(
        f"Datatracker roles for {wg}: " + ", ".join(f"{r.label} {r.name}" for r in out),
        verbose,
        level=LogLevel.STATUS,
    )
    return out
