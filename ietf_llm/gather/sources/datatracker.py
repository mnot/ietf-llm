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

import atexit
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from ... import http_metrics
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get
from ...paths import get_cache_dir

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
# The Datatracker JSON API returns ETags and honours If-None-Match (304).
# We persist {url: {"etag", "body", "last_used"}} so re-gathers revalidate
# cheaply: an unchanged endpoint comes back as an empty 304 and we reuse
# the cached body. This is the bulk of the per-gather metadata chatter
# (document lists, material revisions, the meeting batch, roles). The cache
# is machinery: one shared file at ~/.cache/ietf-llm/.http-cache.json,
# loaded once and flushed atomically at exit.
#
# Eviction (applied at flush) keeps the file from growing without bound as
# many WGs are gathered and paged URLs (id__in= chunks, &offset= pages)
# mint many distinct keys. Every get() bumps the entry's last_used — even on
# a 304 revalidation, since get() runs before the request — so an endpoint
# that's still being gathered, however infrequently, never ages out; only
# genuinely abandoned URLs go stale. The entry cap is a backstop against
# pathological growth and keeps the most-recently-used.

#: Drop ETag entries not requested within this many days (a generous window
#: so an infrequently-gathered WG keeps its cache and doesn't re-download).
_CACHE_MAX_AGE_DAYS = 90
#: Hard cap on entry count, enforced after the age sweep (LRU: newest kept).
_CACHE_MAX_ENTRIES = 10000


class _HttpCache:
    """Lazy ETag store. Loaded from disk on first use and flushed atomically
    (once at interpreter exit for the process-default store, or explicitly by the
    cloud gather-cache sync for a per-corpus store) — so a gather does one write,
    not one per cached URL. Stale and excess entries are evicted at flush time;
    see the module comment above for the policy."""

    def __init__(self, dest: str, *, persist_at_exit: bool = True) -> None:
        # `dest` is captured at construction, not re-resolved later: a flush may
        # be deferred to interpreter exit, and re-resolving get_cache_dir() there
        # would pick up a different cache dir if HOME changed in between (tests
        # revert their HOME monkeypatch before atexit fires).
        self._dest = dest
        self._entries: Optional[Dict[str, Dict[str, Any]]] = None
        self._dirty = False
        # Per-corpus stores are flushed explicitly by the cloud sync, so they
        # skip atexit — that keeps a dropped per-corpus store collectable (an
        # atexit-registered bound method would pin it for the life of the
        # process). The process-default store has no explicit flush, so it does
        # register atexit.
        self._persist_at_exit = persist_at_exit

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._entries is None:
            try:
                with open(self._dest, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._entries = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._entries = {}
        return self._entries

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            if self._persist_at_exit:
                atexit.register(self.flush)

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        entry = self._load().get(url)
        if entry is not None:
            # Touch on read — including the 304 path, which reuses the body
            # without a store() — so an actively-gathered URL stays fresh.
            entry["last_used"] = time.time()
            self._mark_dirty()
        return entry

    def store(self, url: str, etag: str, body: str) -> None:
        self._load()[url] = {"etag": etag, "body": body, "last_used": time.time()}
        self._mark_dirty()

    def _evict(self, now: float) -> None:
        """Drop entries unused past the age window, then cap the count.

        Entries predating last_used tracking (no timestamp) are treated as
        seen `now`, giving legacy caches a full window before they age out.
        """
        entries = self._entries or {}
        cutoff = now - _CACHE_MAX_AGE_DAYS * 86400.0
        kept = {
            url: entry
            for url, entry in entries.items()
            if float(entry.get("last_used", now)) >= cutoff
        }
        if len(kept) > _CACHE_MAX_ENTRIES:
            ranked = sorted(
                kept.items(),
                key=lambda item: float(item[1].get("last_used", 0.0)),
                reverse=True,
            )
            kept = dict(ranked[:_CACHE_MAX_ENTRIES])
        self._entries = kept

    def flush(self) -> None:
        if self._entries is None or not self._dirty:
            return
        self._evict(time.time())
        path = self._dest
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._entries, fh)
            os.replace(tmp, path)
        except OSError:
            pass


#: The process-default ETag store: corpus-agnostic, on persistent disk at the
#: cache root. Used by the local CLI and any gather that has not bound a corpus.
#: Lazy so get_cache_dir() is read on first use, not at import.
_DEFAULT_CACHE: Optional[_HttpCache] = None
_default_lock = threading.Lock()

#: The active per-corpus ETag store for *this thread*. The runner gathers up to
#: N corpora concurrently, one per worker thread; binding the store per thread
#: keeps two concurrent gathers off one shared store and one shared file. Set by
#: the cloud gather-cache sync at hydrate; unset means "use the default store".
_active = threading.local()


def _default_http_cache() -> _HttpCache:
    global _DEFAULT_CACHE  # pylint: disable=global-statement
    with _default_lock:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = _HttpCache(http_cache_path())
        return _DEFAULT_CACHE


def _active_http_cache() -> _HttpCache:
    """This thread's ETag store: a bound per-corpus store if one is set (a
    concurrent gather), else the process-default store."""
    cache = getattr(_active, "cache", None)
    return cache if cache is not None else _default_http_cache()


def http_cache_path(corpus: Optional[str] = None) -> str:
    """Local path of the conditional-GET/ETag cache file for `corpus` (or the
    process-default cache when None). Public so the cloud gather-cache sync
    (`gather.cache_sync`) can round-trip it to durable storage across a
    scale-to-zero wipe (issue #82). A per-corpus path lives *outside* any corpus
    workspace (a dot-prefixed sibling dir), so it is never published as version
    content nor mistaken for a corpus by the cache lister."""
    if corpus:
        return os.path.join(get_cache_dir(), ".http-cache", f"{corpus}.json")
    return os.path.join(get_cache_dir(), ".http-cache.json")


def bind_gather_corpus(corpus: Optional[str]) -> None:
    """Bind this thread's ETag store to `corpus`'s own file — a *fresh* instance,
    so it reloads the just-hydrated per-corpus file rather than carrying stale
    in-memory entries from a prior gather on this reused worker thread. Pass None
    to unbind (back to the process-default store) and release the per-corpus one.
    Called by the cloud gather-cache sync; a no-op concern on the local CLI,
    which never binds and so shares the default store as before."""
    _active.cache = (
        _HttpCache(http_cache_path(corpus), persist_at_exit=False) if corpus else None
    )


def flush_http_cache() -> None:
    """Force this thread's active ETag store to disk now. The store otherwise
    flushes only at interpreter exit, which never fires between gathers in the
    long-lived serve worker — so the cloud sync calls this before persisting."""
    _active_http_cache().flush()


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
    headers = dict(DEFAULT_HEADERS)
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]

    try:
        response = governed_get(url, headers=headers, timeout=timeout)
    except requests.RequestException:
        http_metrics.record(url, 0, 0, error=True)
        return _decode_cached(entry)

    if response.status_code == 304:
        http_metrics.record(url, 304, len(response.content))
        return _decode_cached(entry)
    try:
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        http_metrics.record(
            url, response.status_code, len(response.content), error=True
        )
        return _decode_cached(entry)
    http_metrics.record(url, response.status_code, len(response.content))
    if not isinstance(result, dict):
        return None
    etag = response.headers.get("ETag")
    if etag:
        cache.store(url, etag, response.text)
    return result


def _decode_cached(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Decode a cached response body to a dict, or None."""
    if not entry:
        return None
    try:
        body = json.loads(entry.get("body") or "")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


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
