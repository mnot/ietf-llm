"""IETF group metadata from the Datatracker API.

A thin read layer over `/api/v1/group/…`: fetch a group's record by acronym
(`fetch_group_object`, cached per process) and derive its type / state / name /
parent area / mailing-list name / title / Additional Resources. Used by the
gather sources, the corpus-shape resolver, and the live-lookup read path — so it
sits below all of them, depending only on `net` (the HTTP transport) and
`utils.is_synthetic_wg`.

(Distinct from `gather/sources/datatracker.py`, which is the gather stage that
writes roles + the paginated document listing; this is the shared group-object
read layer.)
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from .net import fetch_resource
from .utils import is_synthetic_wg


@lru_cache(maxsize=128)
def fetch_group_object(wg_name: str) -> Optional[Dict[str, Any]]:
    """Fetch a group's Datatracker record by acronym, or None.

    One JSON call to `/api/v1/group/group/?acronym=<wg>` backs all the
    group-metadata helpers (type, title, mailing list) so we read
    structured fields instead of scraping the group's About page.
    Cached per process, so a single gather resolves each group once
    across charter / drafts / mbox / index / export.

    Synthetic (`x-`) corpora have no Datatracker record, so the lookup
    is skipped entirely (returns None; callers fall back to defaults).
    """
    if is_synthetic_wg(wg_name):
        return None
    url = (
        "https://datatracker.ietf.org/api/v1/group/group/"
        f"?acronym={wg_name}&format=json"
    )
    res = fetch_resource(url)
    if not res:
        return None
    try:
        objects = res.json().get("objects") or []
    except ValueError:
        return None
    return objects[0] if objects else None


@lru_cache(maxsize=128)
def get_group_resources(wg_name: str) -> Tuple[Tuple[str, str, str], ...]:
    """A group's "Additional Resources" as `((slug, label, value), …)`.

    `slug` is the resource type from the extresourcename URI
    (`github_org`, `webpage`, `zulip`, `mailing_list_archive`, …);
    `label` is the human display name ("repositories", "alternate
    list archives", …), falling back to the slug; `value` is its
    URL / string. Empty for synthetic corpora or groups with no
    resources. Read from `/api/v1/group/groupextresource/`, cached
    per run. Returns a tuple so it stays hashable for the cache.
    """
    group = fetch_group_object(wg_name)
    if not group or group.get("id") is None:
        return ()
    url = (
        "https://datatracker.ietf.org/api/v1/group/groupextresource/"
        f"?group={group['id']}&format=json&limit=200"
    )
    res = fetch_resource(url)
    if not res:
        return ()
    try:
        objects = res.json().get("objects") or []
    except ValueError:
        return ()
    out: List[Tuple[str, str, str]] = []
    for obj in objects:
        slug = (obj.get("name") or "").rstrip("/").rsplit("/", 1)[-1]
        value = obj.get("value") or ""
        if slug and value:
            out.append((slug, obj.get("display_name") or slug, value))
    # Sort for deterministic output: group.md is write-if-changed, so a
    # non-deterministic API ordering would churn the file (and re-embed)
    # on every gather.
    return tuple(sorted(out))


#: List name embedded in a mailarchive.ietf.org browse URL.
_MAILARCHIVE_BROWSE_RE = re.compile(
    r"mailarchive\.ietf\.org/arch/browse/([^/?#]+)", re.IGNORECASE
)


def get_mailing_list_name(wg_name: str) -> str:
    """Return the WG's mailing list name for the IMAP archive.

    Normally the local part of the Datatracker `list_email` (e.g.
    `tls` for tls@ietf.org). When the list is hosted off the IETF
    infrastructure — httpbis runs at w3.org — the IETF keeps a mirror
    under a different name; the "alternate list archives" Additional
    Resource points at `mailarchive.ietf.org/arch/browse/<name>/`,
    which is what the IMAP server exposes, so we prefer that `<name>`
    (httpbis → `httpbisa`). Falls back to the WG shortname when no
    record / address is found.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return wg_name
    list_email = group.get("list_email") or ""
    if "@" not in list_email:
        return wg_name
    primary, domain = list_email.split("@", 1)
    if domain.lower() not in ("ietf.org", "irtf.org"):
        for slug, _label, value in get_group_resources(wg_name):
            if slug == "mailing_list_archive":
                match = _MAILARCHIVE_BROWSE_RE.search(value)
                if match:
                    return match.group(1)
    return primary or wg_name


def get_group_type(wg_name: str) -> str:
    """'ietf' for a Working Group, 'irtf' for a Research Group.

    Read from the group's `type` field on Datatracker
    (`.../grouptypename/wg|rg/`). Defaults to 'ietf'.
    """
    group = fetch_group_object(wg_name)
    if group:
        type_uri = (group.get("type") or "").rstrip("/")
        if type_uri.endswith("/rg"):
            return "irtf"
    return "ietf"


def get_group_state(wg_name: str) -> Optional[str]:
    """Group state slug — `active`, `concluded`, `replaced`, … — from
    the Datatracker `state` field, or None when there's no record.

    Worth surfacing because it changes how a consumer reads the
    corpus: a concluded WG won't see new activity, so 'latest thread'
    being old is expected rather than a staleness signal.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return None
    state_uri = (group.get("state") or "").rstrip("/")
    return state_uri.rsplit("/", 1)[-1] or None if state_uri else None


def get_group_name(wg_name: str) -> Optional[str]:
    """The group's human-readable name (e.g. httpbis -> 'HTTP'), or None.

    Persisted into `group.md` so the corpus listing can name a group by
    its title rather than just its shortname, without a network call.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return None
    return (group.get("name") or "").strip() or None


def get_group_area(wg_name: str) -> Optional[Tuple[str, str]]:
    """The group's parent area as `(acronym, name)`, or None.

    Resolves the `parent` link on the group record (e.g. httpbis →
    `('wit', 'Web and Internet Transport')`). Returns None for groups
    with no parent or when the lookup fails.
    """
    group = fetch_group_object(wg_name)
    if not group:
        return None
    parent_uri = group.get("parent")
    if not parent_uri:
        return None
    res = fetch_resource(f"https://datatracker.ietf.org{parent_uri}?format=json")
    if not res:
        return None
    try:
        parent = res.json()
    except ValueError:
        return None
    acronym = parent.get("acronym") or ""
    name = parent.get("name") or ""
    return (acronym, name) if (acronym or name) else None


def get_wg_title(wg_name: str) -> str:
    """Full group name from the IETF Datatracker (e.g. 'Transport Layer
    Security'), or a generic fallback when no record is found."""
    group = fetch_group_object(wg_name)
    if group and group.get("name"):
        return str(group["name"])
    return f"{wg_name.upper()} Working Group"
