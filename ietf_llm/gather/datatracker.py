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
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import requests

from .. import http_metrics
from ..utils import DEFAULT_HEADERS, LogLevel, Verbosity, get_cache_dir, log

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

#: Roles we surface prominently. Everything else collects under "Other".
_LEADERSHIP_ROLES = {"chair", "ad", "techadv", "secr"}


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


def is_leadership(role_slug: str) -> bool:
    return role_slug in _LEADERSHIP_ROLES


# --- API client ------------------------------------------------------------


_EMAIL_FROM_URL = re.compile(r"/api/v1/person/email/([^/]+)/?$")
_ROLE_NAME_FROM_URL = re.compile(r"/api/v1/name/rolename/([^/]+)/?$")


# --- Conditional-GET cache -------------------------------------------------
#
# The Datatracker JSON API returns ETags and honours If-None-Match (304).
# We persist {url: {"etag", "body"}} so re-gathers revalidate cheaply: an
# unchanged endpoint comes back as an empty 304 and we reuse the cached
# body. This is the bulk of the per-gather metadata chatter (document
# lists, material revisions, the meeting batch, roles). The cache is
# machinery: one shared file at ~/.cache/ietf-llm/.http-cache.json,
# loaded once and flushed atomically at exit.


class _HttpCache:
    """Lazy, process-wide ETag store. Loaded from disk on first use and
    flushed once atomically at interpreter exit (so a gather does one
    write, not one per cached URL)."""

    def __init__(self) -> None:
        self._entries: Optional[Dict[str, Dict[str, str]]] = None
        self._dirty = False
        self._dest: Optional[str] = None  # bound once, on first load

    def _path(self) -> str:
        # Resolve the destination exactly once, on first load, while
        # get_cache_dir() still points where the caller intends. The
        # flush is deferred to interpreter exit via atexit; re-resolving
        # get_cache_dir() there would pick up a *different* cache dir
        # whenever HOME changed in between (tests revert their HOME
        # monkeypatch before atexit fires) and write these in-memory
        # entries over the user's real ~/.cache/ietf-llm/.http-cache.json.
        if self._dest is None:
            self._dest = os.path.join(get_cache_dir(), ".http-cache.json")
        return self._dest

    def _load(self) -> Dict[str, Dict[str, str]]:
        if self._entries is None:
            try:
                with open(self._path(), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._entries = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._entries = {}
        return self._entries

    def get(self, url: str) -> Optional[Dict[str, str]]:
        return self._load().get(url)

    def store(self, url: str, etag: str, body: str) -> None:
        self._load()[url] = {"etag": etag, "body": body}
        if not self._dirty:
            self._dirty = True
            atexit.register(self.flush)

    def flush(self) -> None:
        if self._entries is None or not self._dirty:
            return
        path = self._path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._entries, fh)
            os.replace(tmp, path)
        except OSError:
            pass


_HTTP_CACHE = _HttpCache()


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

    entry = _HTTP_CACHE.get(url)
    headers = dict(DEFAULT_HEADERS)
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
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
        _HTTP_CACHE.store(url, etag, response.text)
    return result


def _decode_cached(entry: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
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

    out: List[Role] = []
    # Tiny per-call cache so two chairs of the same WG don't re-fetch
    # the same person endpoint (they don't, but the pattern is cheap).
    person_cache: Dict[str, str] = {}

    for obj in roles_body["objects"]:
        role_url = obj.get("name", "")
        role_match = _ROLE_NAME_FROM_URL.search(role_url)
        if not role_match:
            continue
        role_slug = role_match.group(1)

        email_url = obj.get("email", "")
        email_match = _EMAIL_FROM_URL.search(email_url)
        email = email_match.group(1) if email_match else None

        person_url = obj.get("person", "")
        if person_url in person_cache:
            name = person_cache[person_url]
        else:
            person_body = _get_json(person_url)
            name = ""
            if person_body:
                name = person_body.get("name") or person_body.get("ascii") or ""
            if not name and email:
                name = email.split("@", 1)[0]
            person_cache[person_url] = name

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
