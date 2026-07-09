"""Consumer side of the seed store: read a remote index, download + verify +
install a corpus bundle into the local cache (issue #182).

Gather-path only — this reaches the network and writes the cache, so it is
imported lazily by `gather.sequencer` and never by the read-only serve path.
Supports a plain filesystem path, a `file://` URL, or an `https://` URL for the
seed base, so tests (and an operator's own mirror) run offline. The compatibility
*decision* lives with the caller (which knows the client's embedding model); this
module owns the mechanics: load, verify, atomically install, and stamp provenance.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import uuid
from typing import Any, Dict, Optional

from .. import freshness
from ..paths import get_cache_dir, get_index_dir
from . import format as fmt

#: Network read timeout (seconds) for an http(s) seed base.
_HTTP_TIMEOUT = 30


class SeedFetchError(Exception):
    """A seed base could not be read, or a bundle failed to download/verify."""


# --------------------------------------------------------------------------- #
# Location plumbing (filesystem path | file:// | https://)
# --------------------------------------------------------------------------- #


def _is_url(location: str) -> bool:
    return "://" in location


def _child(base: str, rel: str) -> str:
    """Locate a store-relative path under `base`, whether `base` is a URL or a
    local filesystem path."""
    if _is_url(base):
        return base.rstrip("/") + "/" + rel.lstrip("/")
    return os.path.join(base, *rel.split("/"))


def _read_bytes(location: str, timeout: Optional[float] = None) -> bytes:
    """Read a small resource (index/manifest) fully into memory."""
    try:
        if _is_url(location):
            with urllib.request.urlopen(  # nosec B310 — operator/user-set base
                location, timeout=_HTTP_TIMEOUT if timeout is None else timeout
            ) as resp:
                return bytes(resp.read())
        with open(location, "rb") as handle:
            return handle.read()
    except OSError as err:
        raise SeedFetchError(f"cannot read {location}: {err}") from err


def _download(location: str, dest_path: str) -> None:
    """Stream a bundle to `dest_path` (its dir must exist)."""
    try:
        if _is_url(location):
            with (
                urllib.request.urlopen(  # nosec B310 — operator/user-set base
                    location, timeout=_HTTP_TIMEOUT
                ) as resp,
                open(dest_path, "wb") as out,
            ):
                shutil.copyfileobj(resp, out)
        else:
            shutil.copyfile(location, dest_path)
    except OSError as err:
        raise SeedFetchError(f"cannot download {location}: {err}") from err


# --------------------------------------------------------------------------- #
# Index / manifest
# --------------------------------------------------------------------------- #


def load_index(seed_url: str, timeout: Optional[float] = None) -> Optional[fmt.Index]:
    """Fetch and parse the store's `index.json`. **Best-effort**: returns None on
    any failure (unreachable, malformed, unsupported format) so a gather degrades
    to a cold gather rather than erroring — the seed store only ever accelerates.
    `timeout` bounds the HTTP read (default `_HTTP_TIMEOUT`); the catalog refresh
    passes a short one so a read tool never hangs on a slow mirror."""
    try:
        raw = _read_bytes(_child(seed_url, fmt.INDEX_NAME), timeout=timeout)
        return fmt.Index.from_json(raw.decode("utf-8"))
    except (SeedFetchError, fmt.SeedFormatError, UnicodeDecodeError):
        return None


def load_manifest(seed_url: str, entry: fmt.IndexEntry) -> fmt.Manifest:
    """Fetch and parse one corpus's manifest. Raises `SeedFetchError` /
    `SeedFormatError` (the caller soft-fails)."""
    raw = _read_bytes(_child(seed_url, entry.manifest))
    return fmt.manifest_from_json(raw.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #


def install(seed_url: str, entry: fmt.IndexEntry) -> str:
    """Download, verify, and install the bundle for `entry` into the local cache,
    atomically. Returns the installed version.

    Replaces the corpus's cache tree wholesale (cold install and stale-jump take
    the same path); a killed install never leaves a torn tree. `imap-cache/` and
    config live outside the corpus dir and are untouched. Raises `SeedFetchError`
    / `SeedFormatError` on any failure — the caller (gather) soft-fails to a cold
    gather."""
    corpus = entry.name
    manifest = load_manifest(seed_url, entry)
    # Stage under the cache dir (not the system temp, which is often tmpfs on a
    # different filesystem) so the install swap is a true same-filesystem
    # os.rename — atomic, and never an EXDEV fall back to a non-atomic copy that
    # could leave a torn corpus.
    os.makedirs(get_cache_dir(), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ietf-seed-", dir=get_cache_dir()) as tmp:
        bundle_path = os.path.join(tmp, "bundle.tar.gz")
        _download(_child(seed_url, manifest.bundle), bundle_path)
        fmt.verify_sha256(bundle_path, manifest.bundle_sha256)
        staging = os.path.join(tmp, "tree")
        fmt.extract_bundle(bundle_path, staging)
        _install_tree(corpus, staging)
    _write_seed_source(corpus, seed_url, manifest)
    return manifest.version


def _install_tree(corpus: str, staging: str) -> None:
    """Move `staging` (a materialised version tree) into place as `corpus`'s cache
    tree. When `IETF_LLM_INDEX_DIR` splits the index onto its own volume, relocate
    the top-level index files there first so only `files/` + manifests swap into
    the corpus dir."""
    corpus_dir = os.path.join(get_cache_dir(), corpus)
    index_dir = os.path.join(get_index_dir(), corpus)
    if os.path.realpath(index_dir) != os.path.realpath(corpus_dir):
        os.makedirs(index_dir, exist_ok=True)
        for name in fmt.INDEX_FILES:
            src = os.path.join(staging, name)
            if os.path.isfile(src):
                dst = os.path.join(index_dir, name)
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)
    _swap_dir(staging, corpus_dir)


def _swap_dir(staging: str, dest: str) -> None:
    """Atomically replace `dest` with `staging` via `os.rename`. `install` stages
    under the cache dir, so `staging` and `dest` are always on one filesystem and
    the rename never crosses filesystems (no EXDEV, no non-atomic copy).

    Move any existing tree aside first and restore it if the rename fails, so a
    failed — or killed — re-seed never destroys a good corpus."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    old: Optional[str] = None
    if os.path.exists(dest):
        old = f"{dest}.old.{uuid.uuid4().hex[:8]}"
        os.rename(dest, old)
    try:
        os.rename(staging, dest)
    except OSError as err:
        if old is not None:
            os.rename(old, dest)  # restore the prior corpus; rename left dest absent
            old = None
        raise SeedFetchError(f"cannot install {dest}: {err}") from err
    finally:
        if old is not None and os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)


def _write_seed_source(corpus: str, seed_url: str, manifest: fmt.Manifest) -> None:
    """Stamp the provenance sentinel (best-effort). It lives in `freshness` (a
    read-safe leaf) so the read tools can surface it without importing this
    gather-path module."""
    freshness.record_seed_source(
        corpus, url=seed_url, version=manifest.version, gathered=manifest.gathered
    )


def seed_source(corpus: str) -> Optional[Dict[str, Any]]:
    """The provenance recorded when `corpus` was last seeded, or None."""
    return freshness.seed_source(corpus)
