"""Find and download the newest semantic index rfc.fyi has published.

rfc.fyi builds the index locally and publishes it as a GitHub release asset
under a build-stamped tag (`index-20260811T003915Z`), because quantised
vectors do not delta-compress and committing a regenerated tree monthly
would grow that repo without bound. Discovery is therefore "list the
releases and take the newest `index-` one" — the same idiom rfc.fyi's own
Pages workflow uses to stitch the index into a deploy.

Two properties make this cheap and safe to depend on:

**Anonymous.** The releases API and the asset are public reads, so no token
is needed. Only the *publisher* calls this — monthly, when re-bundling for
the seed store — so the 60-requests-per-hour anonymous limit is never near.

**Tags sort.** `manifest.build` is a normalised UTC stamp with no
separators, precisely so it is a legal git ref; lexical order over those
tags is chronological order. We sort rather than trusting the API's
ordering, since "newest" there is by creation time, which a re-tagged or
back-filled release would not respect.

Publisher-side only — see the subpackage docstring.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..log import LogLevel, Verbosity, log
from ..net import DEFAULT_HEADERS, governed_get
from ..seed.format import SeedFormatError, extract_bundle
from .format import IndexManifest, RfcIndexError, read_manifest

#: The repository publishing the index. A parameter rather than a constant at
#: the call site so a mirror or a fork can be pointed at in a test.
DEFAULT_REPO = "mnot/rfc.fyi"

#: Release tags that carry an index. Anything else in the repo's releases is
#: not ours to interpret.
TAG_PREFIX = "index-"

#: The asset within such a release.
ASSET_NAME = "index.tar.gz"

#: Where the tarball unpacks to. `make index-release` runs `tar czf … index`
#: from the repo root, so every member is prefixed with this.
ARCHIVE_ROOT = "index"

_API = "https://api.github.com/repos/{repo}/releases"

#: The releases listing is small (the publisher prunes to two) but the asset
#: is ~130 MB, so they get different patience.
_LIST_TIMEOUT = 30
_ASSET_TIMEOUT = 300


@dataclass(frozen=True)
class IndexRelease:
    """A published index: its tag, the build id that tag encodes, and the
    asset to fetch."""

    tag: str
    build: str
    asset_url: str
    asset_bytes: int


def _releases(repo: str) -> List[Dict[str, Any]]:
    url = _API.format(repo=repo)
    response = governed_get(url, headers=dict(DEFAULT_HEADERS), timeout=_LIST_TIMEOUT)
    if response.status_code != 200:
        raise RfcIndexError(f"{url}: HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, list):
        raise RfcIndexError(f"{url}: expected a list of releases")
    return [item for item in body if isinstance(item, dict)]


def _as_release(item: Dict[str, Any]) -> Optional[IndexRelease]:
    """One API entry as an `IndexRelease`, or None if it isn't one of ours."""
    tag = str(item.get("tag_name") or "")
    if not tag.startswith(TAG_PREFIX):
        return None
    # A draft is not published (its asset needs auth) and a prerelease is by
    # definition not the one to build a seed bundle from.
    if item.get("draft") or item.get("prerelease"):
        return None
    for asset in item.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == ASSET_NAME:
            url = str(asset.get("browser_download_url") or "")
            if not url:
                return None
            return IndexRelease(
                tag=tag,
                build=tag[len(TAG_PREFIX) :],
                asset_url=url,
                asset_bytes=int(asset.get("size") or 0),
            )
    return None


def latest_release(repo: str = DEFAULT_REPO) -> Optional[IndexRelease]:
    """The newest published index release, or None if the repo has none.

    None is a real answer, not an error: rfc.fyi's own deploy treats a
    missing index as "publish without full-text search", and a consumer that
    has nothing to re-bundle should say so rather than fail.
    """
    candidates = [r for r in (_as_release(item) for item in _releases(repo)) if r]
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: r.build)[-1]


def download_index(
    release: IndexRelease, dest_dir: str, verbosity: Verbosity = Verbosity.STATUS
) -> str:
    """Fetch and unpack `release` under `dest_dir`; return the index directory.

    The tarball is verified before it is returned — `read_manifest` refuses a
    truncated or unknown-version index, and the build it declares is checked
    against the tag it was published under. A mismatch there means the release
    was mis-tagged, which is exactly the case where trusting the tag (the only
    thing a consumer selects on) would pin the wrong build indefinitely.
    """
    os.makedirs(dest_dir, exist_ok=True)
    log(
        f"fetching {release.tag} ({release.asset_bytes / 1048576:.0f} MB)",
        verbosity,
        LogLevel.PROGRESS,
    )
    response = governed_get(
        release.asset_url, headers=dict(DEFAULT_HEADERS), timeout=_ASSET_TIMEOUT
    )
    if response.status_code != 200:
        raise RfcIndexError(f"{release.asset_url}: HTTP {response.status_code}")
    handle, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=dest_dir)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(response.content)
        try:
            # Reuse the seed store's guarded extractor rather than writing a
            # second one: it is a plain tar of regular files either way, and
            # the path-escape / non-regular-member refusals are the same
            # refusals we want here.
            extract_bundle(tmp, dest_dir)
        except SeedFormatError as err:
            raise RfcIndexError(f"{release.tag}: {err}") from err
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    index_dir = os.path.join(dest_dir, ARCHIVE_ROOT)
    manifest = read_manifest(index_dir)
    if manifest.build != release.build:
        raise RfcIndexError(
            f"{release.tag}: manifest declares build {manifest.build!r}, "
            f"the tag says {release.build!r}"
        )
    return index_dir


def manifest_for(index_dir: str) -> IndexManifest:
    """Convenience re-export so a caller needs only this module."""
    return read_manifest(index_dir)
