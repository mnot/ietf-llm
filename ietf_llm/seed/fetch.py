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
from typing import Any, Dict, List, Optional, Tuple

from .. import freshness
from ..atomicio import stage_split_dir, swap_dirs
from ..net import DEFAULT_HEADERS
from ..paths import get_cache_dir, get_index_dir
from ..tls import system_trust_context
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


def _request(location: str) -> urllib.request.Request:
    """A GET carrying the project's `User-Agent`. The seed base can be any
    operator's mirror, so identify the client and its contact path here exactly
    as `net.transport` does for the gather fetches — this module streams bundles
    and so stays on urllib rather than the shared `requests` session."""
    return urllib.request.Request(location, headers=dict(DEFAULT_HEADERS))


def _read_bytes(location: str, timeout: Optional[float] = None) -> bytes:
    """Read a small resource (index/manifest) fully into memory."""
    try:
        if _is_url(location):
            with urllib.request.urlopen(  # nosec B310 — operator/user-set base
                _request(location),
                timeout=_HTTP_TIMEOUT if timeout is None else timeout,
                context=system_trust_context(),
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
                    _request(location),
                    timeout=_HTTP_TIMEOUT,
                    context=system_trust_context(),
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


def read_index(seed_url: str, timeout: Optional[float] = None) -> fmt.Index:
    """Fetch and parse the store's `index.json`, **raising** on failure
    (`SeedFetchError` / `SeedFormatError` / `UnicodeDecodeError`).

    Most callers want `load_index`, which soft-fails. This one is for the caller
    that has to *report* why there is no index — a soft failure is unhelpful when
    the whole job is telling the user what went wrong (`ietf-llm --init`)."""
    raw = _read_bytes(_child(seed_url, fmt.INDEX_NAME), timeout=timeout)
    return fmt.Index.from_json(raw.decode("utf-8"))


def load_index(seed_url: str, timeout: Optional[float] = None) -> Optional[fmt.Index]:
    """Fetch and parse the store's `index.json`. **Best-effort**: returns None on
    any failure (unreachable, malformed, unsupported format) so a gather degrades
    to a cold gather rather than erroring — the seed store only ever accelerates.
    `timeout` bounds the HTTP read (default `_HTTP_TIMEOUT`); the catalog refresh
    passes a short one so a read tool never hangs on a slow mirror."""
    try:
        return read_index(seed_url, timeout=timeout)
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
    tree. When `IETF_LLM_INDEX_DIR` splits the index onto its own volume, the
    index files swap in first (staged into their own temp dir, seeded with
    whatever `index_dir` isn't being replaced — the same split
    `CloudCorpusStore.seed_workspace` uses, issue #224); if that swap lands but
    the corpus-dir swap then fails, the index swap is unwound too, rather than
    pairing a new index with old content, or the reverse.

    Raises `SeedFetchError` on any failure — staging the index files or
    swapping either directory into place."""
    corpus_dir = os.path.join(get_cache_dir(), corpus)
    index_dir = os.path.join(get_index_dir(), corpus)
    index_tmp: Optional[str] = None
    try:
        if os.path.realpath(index_dir) != os.path.realpath(corpus_dir):
            new_names = {
                name
                for name in fmt.INDEX_FILES
                if os.path.isfile(os.path.join(staging, name))
            }
            # Nothing to relocate: leave index_dir untouched rather than
            # staging and swapping it for an unchanged copy of itself.
            # Mirrors `CloudCorpusStore.seed_workspace`'s identical split
            # (issue #224) via the same shared staging helper.
            if new_names:
                index_tmp = stage_split_dir(index_dir, "install", staging, new_names)
        os.makedirs(os.path.dirname(corpus_dir) or ".", exist_ok=True)

        # Both new trees (and their parents) are ready; only the swaps
        # remain. Index first, then the corpus dir, mirroring
        # `seed_workspace`'s ordering. `swap_dirs` treats the two as one
        # unit: if the later swap fails, the first is undone too, so an
        # install either lands as a whole or leaves corpus_dir/index_dir
        # exactly as they were.
        swaps: List[Tuple[str, str]] = []
        if index_tmp is not None:
            swaps.append((index_dir, index_tmp))
        swaps.append((corpus_dir, staging))
        swap_dirs(swaps)
    except OSError as err:
        raise SeedFetchError(f"cannot install {corpus_dir}: {err}") from err
    finally:
        if index_tmp is not None and os.path.isdir(index_tmp):
            shutil.rmtree(index_tmp, ignore_errors=True)


def _write_seed_source(corpus: str, seed_url: str, manifest: fmt.Manifest) -> None:
    """Stamp the provenance sentinel (best-effort). It lives in `freshness` (a
    read-safe leaf) so the read tools can surface it without importing this
    gather-path module."""
    freshness.record_seed_source(
        corpus, url=seed_url, version=manifest.version, gathered=manifest.gathered
    )


def seed_source(corpus: str) -> Optional[Dict[str, Any]]:
    """The provenance recorded when `corpus` was last seeded, or None.

    Local-only: reads straight off local disk, matching what
    `_write_seed_source` just wrote there, rather than bouncing through the
    seam (which could report a different backend's version under
    `IETF_LLM_STORE_BACKEND=cloud`)."""
    return freshness.local_seed_source(corpus)
