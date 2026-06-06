"""Mirror the active-effort catalog from Datatracker into the cache.

Datatracker's group collection is the canonical list of IETF/IRTF
efforts. Rather than hit it per query, we mirror the active (and BoF)
slice into `~/.cache/ietf-llm/_catalog/`, where `catalog.render_efforts`
reads it network-free and ranks efforts by topic.

This is a singleton, not a per-corpus artifact: `ensure_catalog_index`
runs once per `ietf-llm` invocation (see `__main__.main`), invisibly,
and is cheap to call — same discipline as the RFC mirror (`gather.rfcs`):

  - **TTL guard.** If the derived `catalog.json` is younger than
    `CATALOG_TTL_SECONDS` we don't touch the network at all.
  - **Conditional GET.** Past the TTL we revalidate each source with
    `If-None-Match` from a `.etag` sidecar; an unchanged source comes
    back 304 with no body. We only rebuild `catalog.json` when a source
    actually changed (or it's missing), otherwise just touch its mtime.

Unlike the RFC mirror, the reader-facing file is *derived*: we keep the
raw source payloads (`raw-active.json`, `raw-bof.json`) for revalidation,
then project them down to the slim record list the reader wants —
filtering to working/research groups, resolving each effort's parent
area from the area objects in the same payload.

Best-effort throughout: any network or write failure leaves the existing
cache in place and logs at PROGRESS. A missing catalog is not an error —
the reader degrades to a "not gathered yet" message.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..catalog import CATALOG_FILE, catalog_index_dir
from ..utils import DEFAULT_HEADERS, LogLevel, Verbosity, governed_get, log
from . import _mirror

_API = "https://datatracker.ietf.org/api/v1/group/group/"

#: Source slices to mirror, each a (local-name, query) pair. `limit=0`
#: returns the whole slice in one response; both are well under
#: Datatracker's 1000-row cap (active ≈ 465, bof ≈ 1), so no paging.
_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("raw-active.json", f"{_API}?state=active&format=json&limit=0"),
    ("raw-bof.json", f"{_API}?state=bof&format=json&limit=0"),
)

#: The effort types we keep — chartered working and research groups.
#: (BoFs live in the active/bof slices as `wg`-typed groups in a `bof`
#: state, so this filter keeps them too.)
_EFFORT_TYPES = frozenset({"wg", "rg"})

#: Revalidate at most once per day; the group list changes slowly.
CATALOG_TTL_SECONDS = 24 * 60 * 60

_TIMEOUT = 30


def ensure_catalog_index(
    verbosity: Verbosity = Verbosity.STATUS, force: bool = False
) -> None:
    """Refresh the local effort-catalog mirror if stale. Never raises."""
    target_dir = catalog_index_dir()
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as err:
        log(
            f"Catalog: cannot create {target_dir}: {err}",
            verbosity,
            LogLevel.PROGRESS,
        )
        return
    catalog_path = os.path.join(target_dir, CATALOG_FILE)
    if not force and _mirror.is_fresh(catalog_path, CATALOG_TTL_SECONDS):
        return
    changed = False
    for name, url in _SOURCES:
        changed = _refresh_source(target_dir, name, url, verbosity) or changed
    if changed or not os.path.exists(catalog_path):
        _rebuild_catalog(target_dir, catalog_path, verbosity)
    else:
        _mirror.touch(catalog_path)


def _refresh_source(target_dir: str, name: str, url: str, verbosity: Verbosity) -> bool:
    """Conditional-GET one source into `<name>` (+ `.etag` sidecar).

    Returns True if a fresh body was written (a 200), False on a 304 or
    any failure — i.e. True means the derived catalog needs a rebuild.
    """
    body_path = os.path.join(target_dir, name)
    etag_path = body_path + ".etag"
    headers = dict(DEFAULT_HEADERS)
    etag = _mirror.read_etag(etag_path) if os.path.exists(body_path) else None
    if etag:
        headers["If-None-Match"] = etag
    try:
        response = governed_get(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as err:
        log(f"Catalog: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return False
    if response.status_code == 304:
        return False
    try:
        response.raise_for_status()
    except requests.RequestException as err:
        log(f"Catalog: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return False
    if not _mirror.write_body(body_path, response.content, verbosity, "Catalog"):
        return False
    _mirror.write_sidecar(etag_path, response.headers.get("ETag"))
    log(f"Catalog: updated {name}", verbosity, LogLevel.PROGRESS)
    return True


def _rebuild_catalog(target_dir: str, catalog_path: str, verbosity: Verbosity) -> None:
    """Project the raw source payloads down to the slim record list and
    write `catalog.json` atomically. Best-effort: a parse failure leaves
    any existing catalog in place."""
    objects: List[Dict[str, Any]] = []
    for name, _ in _SOURCES:
        objects.extend(_read_objects(os.path.join(target_dir, name)))
    if not objects:
        log("Catalog: no source records to build from", verbosity, LogLevel.PROGRESS)
        return
    areas = _area_map(objects)
    seen: set[str] = set()
    efforts: List[Dict[str, Any]] = []
    for obj in objects:
        if _slug(obj.get("type")) not in _EFFORT_TYPES:
            continue
        acronym = obj.get("acronym") or ""
        if not acronym or acronym in seen:
            continue
        seen.add(acronym)
        efforts.append(_project(obj, areas))
    efforts.sort(key=lambda e: e["acronym"])
    payload = json.dumps(efforts, ensure_ascii=False).encode("utf-8")
    if _mirror.write_body(catalog_path, payload, verbosity, "Catalog"):
        log(f"Catalog: built {len(efforts)} efforts", verbosity, LogLevel.PROGRESS)


def _project(obj: Dict[str, Any], areas: Dict[str, Tuple[str, str]]) -> Dict[str, Any]:
    """One Datatracker group record -> one slim catalog record."""
    area_acr, area_name = areas.get(obj.get("parent") or "", ("", ""))
    return {
        "acronym": obj.get("acronym") or "",
        "name": obj.get("name") or "",
        "type": _slug(obj.get("type")),
        "state": _slug(obj.get("state")),
        "area": area_acr,
        "area_name": area_name,
        "description": (obj.get("description") or "").strip(),
    }


def _area_map(objects: List[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """`resource_uri` -> (acronym, name) for every group in the payload, so
    an effort's `parent` link resolves without a fetch. Keyed on all
    groups (not just `area`-typed ones) so a research group whose parent
    is the IRTF top group resolves to `irtf`, not blank."""
    areas: Dict[str, Tuple[str, str]] = {}
    for obj in objects:
        uri = obj.get("resource_uri")
        if uri:
            areas[uri] = (obj.get("acronym") or "", obj.get("name") or "")
    return areas


def _slug(uri: Optional[str]) -> str:
    """Last path segment of a Datatracker name URI: `.../wg/` -> `wg`."""
    if not uri:
        return ""
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _read_objects(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return []
    objects = loaded.get("objects") if isinstance(loaded, dict) else None
    return (
        [o for o in objects if isinstance(o, dict)] if isinstance(objects, list) else []
    )
