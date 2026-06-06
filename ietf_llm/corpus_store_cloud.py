"""The cloud CorpusStore backend: a control plane + a blob plane composed.

`CloudCorpusStore` is the CorpusStore implementation a cloud deployment uses.
It holds no storage logic of its own — it composes a `ControlPlane`
(transactional versions / pointer / leases) and a `BlobStore` (immutable
whole-object blobs), and stages materialised versions onto a local scratch
directory. Both planes are pluggable: the control plane is a `SqlControlPlane`
over a `SqlExecutor` (a local SQLite file, or a SQLite-compatible cloud database
over HTTP such as Cloudflare D1); the blob plane is `file://`. This class is
backend-agnostic — it depends only on the `ControlPlane` and `BlobStore`
interfaces. See `docs/storage.md`.

Publish ordering is the load-bearing invariant: blobs go to a *fresh* version
prefix first (invisible — nothing references it), then the control plane records
the version and flips the pointer in one transaction. An interruption before
that flip leaves the prior version current and the staged blobs orphaned, never
a torn read.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .corpus_blobs import BlobStore, FileBlobStore
from .corpus_control import ControlPlane, SqliteControlPlane
from .corpus_store import CorpusStore, pinned_version, pinned_versions_in_use

#: Process-global current-version cache: (cache_key, corpus) -> (version,
#: monotonic expiry). Keyed by the control-plane identity (its locator) so two
#: stores over different control planes in one process never share entries.
#: Caches None too (negative caching), so a burst of lookups for an absent
#: corpus also collapses to one control-plane call. Off unless a positive TTL is
#: configured. See `service_config.resolve_ttl`.
_RESOLVE_CACHE: Dict[Tuple[str, str], Tuple[Optional[str], float]] = {}
_RESOLVE_LOCK = threading.Lock()


def _clear_resolve_cache() -> None:
    """Drop every cached current-version entry (test seam / hard reset)."""
    with _RESOLVE_LOCK:
        _RESOLVE_CACHE.clear()


def _new_version() -> str:
    """A unique, lexically-sortable version token: a UTC timestamp plus a short
    random suffix so two publishes in the same second do not collide."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _build_control_plane(locator: str) -> ControlPlane:
    """Build the control-plane backend from `locator`'s scheme: `d1://…` selects
    the Cloudflare D1 adapter; a filesystem path selects the local SQLite
    backend."""
    if locator.startswith("d1://"):
        from .corpus_control_d1 import (  # pylint: disable=import-outside-toplevel
            D1ControlPlane,
        )

        return D1ControlPlane(locator)
    return SqliteControlPlane(locator)


def _build_blob_store(locator: str) -> BlobStore:
    """Build the blob backend from `locator`'s scheme: `s3://bucket/prefix`
    selects the S3-compatible backend (the `[s3]` extra); a directory path
    selects the bundled `file://` backend."""
    if locator.startswith("s3://"):
        try:
            from .corpus_blobs_s3 import (  # pylint: disable=import-outside-toplevel
                S3BlobStore,
            )
        except ImportError as err:
            raise ValueError(
                "an s3:// blob store needs the 's3' extra (pip install ietf-llm[s3])"
            ) from err
        return S3BlobStore(locator)
    return FileBlobStore(locator)


def build_cloud_store() -> "CloudCorpusStore":
    """Construct the cloud backend from service config, or raise ValueError if
    it is selected but under-configured. The serve path surfaces this earlier
    via boot-time validation; this guards the CLI / gather path too."""
    from . import service_config  # pylint: disable=import-outside-toplevel

    control = service_config.control_db()
    blobs = service_config.blob_dir()
    scratch = service_config.scratch_dir()
    missing = [
        env
        for env, value in (
            ("IETF_LLM_CONTROL_DB", control),
            ("IETF_LLM_BLOB_DIR", blobs),
            ("IETF_LLM_SCRATCH_DIR", scratch),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "cloud corpus store selected but not configured: missing "
            + ", ".join(missing)
        )
    assert control and blobs and scratch  # narrowed by the check above
    return CloudCorpusStore(
        _build_control_plane(control),
        _build_blob_store(blobs),
        scratch,
        resolve_ttl=service_config.resolve_ttl(),
        cache_key=control,
    )


class CloudCorpusStore(CorpusStore):
    """CorpusStore over a transactional control plane + an immutable blob store,
    materialising versions onto `scratch_dir`."""

    def __init__(
        self,
        control: ControlPlane,
        blobs: BlobStore,
        scratch_dir: str,
        *,
        resolve_ttl: float = 0.0,
        cache_key: str = "",
    ) -> None:
        self._control = control
        self._blobs = blobs
        self._scratch = scratch_dir
        # Current-version caching is opt-in: off (ttl 0) for direct construction,
        # configured to a short TTL by `build_cloud_store`. `cache_key` scopes the
        # process-global cache to this control plane.
        self._resolve_ttl = resolve_ttl
        self._cache_key = cache_key

    def list_corpora(self) -> List[str]:
        return self._control.list_corpora()

    def resolve_current(self, corpus: str) -> Optional[str]:
        # A short-TTL cache so a burst of reads coalesces to one control-plane
        # call (the cloud control plane is a per-request HTTP hop, and the D1
        # REST path shares the account-wide API rate limit). Versions are
        # immutable and the pointer changes only on publish, so a stale hit just
        # serves a valid older version for up to the TTL; the publishing process
        # refreshes its own entry immediately (see `publish`). Concurrent misses
        # may each resolve within the window — no single-flight, which a short
        # TTL makes cheap.
        if self._resolve_ttl <= 0:
            return self._control.resolve_current(corpus)
        key = (self._cache_key, corpus)
        with _RESOLVE_LOCK:
            entry = _RESOLVE_CACHE.get(key)
            if entry is not None and entry[1] > time.monotonic():
                return entry[0]
        # Resolve outside the lock so a slow control-plane call doesn't serialise
        # every reader.
        version = self._control.resolve_current(corpus)
        with _RESOLVE_LOCK:
            _RESOLVE_CACHE[key] = (version, time.monotonic() + self._resolve_ttl)
        return version

    def _cache_version(self, corpus: str, version: Optional[str]) -> None:
        """Write `version` straight into the current-version cache (write-through
        on publish), so the publishing process serves what it just published with
        no extra control-plane round trip. No-op when caching is off."""
        if self._resolve_ttl <= 0:
            return
        with _RESOLVE_LOCK:
            _RESOLVE_CACHE[(self._cache_key, corpus)] = (
                version,
                time.monotonic() + self._resolve_ttl,
            )

    def local_cache_dir(self, corpus: str) -> Optional[str]:
        # Honour a request-scoped pin so all of a request's reads stay on one
        # version even if a publish lands mid-request (G-1); otherwise resolve
        # through the cache.
        version = pinned_version(corpus) or self.resolve_current(corpus)
        if version is None:
            return None
        dest_root = os.path.join(self._scratch, corpus, version)
        # Versions are immutable, so a materialised copy is reusable: only fetch
        # if this version is not already staged locally.
        if not os.path.isdir(dest_root):
            self._materialise_version(corpus, version, dest_root)
        files_dir = os.path.join(dest_root, "files")
        return files_dir if os.path.isdir(files_dir) else None

    def local_index_dir(self, corpus: str) -> Optional[str]:
        version = pinned_version(corpus) or self.resolve_current(corpus)
        if version is None:
            return None
        dest_root = os.path.join(self._scratch, corpus, version)
        # Materialise the version (idempotent — shared with local_cache_dir);
        # the version's `embeddings.db` sits directly under dest_root.
        if not os.path.isdir(dest_root):
            self._materialise_version(corpus, version, dest_root)
        return dest_root

    def _materialise_version(self, corpus: str, version: str, dest_root: str) -> None:
        """Materialise a version onto local scratch **atomically and verified**:
        fetch into a temp dir, check every file the manifest lists is present,
        then rename into place. So a present `dest_root` is always a complete,
        verified copy — a crash or a concurrent fetch cannot leave a partial tree
        that a reader serves as if whole, and a lost blob fails loudly rather than
        silently dropping a file from the materialised tree."""
        manifest = self._control.get_manifest(corpus, version) or {}
        expected = list(manifest.get("files") or [])
        tmp = f"{dest_root}.tmp.{uuid.uuid4().hex[:8]}"
        try:
            self._blobs.materialise_prefix(f"{corpus}/{version}/", tmp)
            missing = [f for f in expected if not os.path.isfile(os.path.join(tmp, f))]
            if missing:
                raise FileNotFoundError(
                    f"version {version} of '{corpus}' is incomplete: missing "
                    + ", ".join(sorted(missing)[:5])
                )
            os.makedirs(os.path.dirname(dest_root), exist_ok=True)
            try:
                os.rename(tmp, dest_root)
            except OSError:
                # Another materialise won the race and dest_root now exists; use
                # it. Re-raise only if it genuinely failed to appear.
                if not os.path.isdir(dest_root):
                    raise
        finally:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
        # A new version just landed on this replica's scratch; sweep older ones.
        self._reap_scratch(corpus, version)

    def _reap_scratch(self, corpus: str, current_version: str) -> None:
        """Delete this replica's materialised version dirs for `corpus` that are
        no longer in use, bounding scratch (otherwise every gather leaves one
        full corpus copy on local disk forever — RAM, if scratch is tmpfs).

        Keeps the version just materialised, the resolve-cache current one (about
        to be served to imminent reads), and any pinned by an in-flight request —
        so a live read is never reaped. Best-effort: a failure never breaks a
        read, and reaping is non-destructive anyway — blobs are immutable and
        retained, so a reaped version re-materialises on demand if read again.
        (Scratch is per-replica, so each replica reaps its own; orphaned *blobs*
        are a separate operator concern — a bucket lifecycle rule. See
        `docs/storage.md`.)"""
        keep = {current_version} | pinned_versions_in_use(corpus)
        with _RESOLVE_LOCK:
            cached = _RESOLVE_CACHE.get((self._cache_key, corpus))
        if cached and cached[0]:
            keep.add(cached[0])
        corpus_root = os.path.join(self._scratch, corpus)
        try:
            names = os.listdir(corpus_root)
        except OSError:
            return
        for name in names:
            # Skip in-use versions and any `.tmp.` staging dir (an in-progress or
            # crashed fetch; the owning materialise cleans its own).
            if name in keep or ".tmp." in name:
                continue
            full = os.path.join(corpus_root, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)

    def publish(
        self,
        corpus: str,
        workspace: str,
        version: Optional[str] = None,
        *,
        extra_files: Optional[Dict[str, str]] = None,
    ) -> str:
        version = version or _new_version()
        prefix = f"{corpus}/{version}/"
        staged: Dict[str, str] = {}
        # Stage every file in the workspace to the fresh version prefix, plus any
        # extra_files (e.g. an index living outside the cache), keyed by their
        # version-relative path. Nothing references this prefix yet, so a partial
        # upload is invisible. Workspace files win on a key clash.
        for rel, abs_path in (extra_files or {}).items():
            staged[rel.replace(os.sep, "/")] = abs_path
        for dirpath, _dirs, names in os.walk(workspace):
            for name in names:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, workspace).replace(os.sep, "/")
                staged[rel] = abs_path
        files: List[str] = []
        for rel, abs_path in staged.items():
            with open(abs_path, "rb") as handle:
                self._blobs.put(prefix + rel, handle.read())
            files.append(rel)
        # Atomically record the version and flip the pointer. If this raises,
        # the staged blobs are orphaned and the prior version stays current.
        self._control.publish_version(
            corpus, version, {"version": version, "files": sorted(files)}
        )
        # Write-through so this process serves the version it just published
        # immediately, without waiting out the resolve TTL. Other replicas /
        # processes pick it up within the TTL.
        self._cache_version(corpus, version)
        return version

    def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        return self._control.acquire_lease(corpus, owner, ttl)

    def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        return self._control.renew_lease(corpus, owner, ttl)

    def release_lease(self, corpus: str, owner: str) -> None:
        self._control.release_lease(corpus, owner)

    def acquire_gather_slot(
        self, owner: str, corpus: str, ttl: float, max_inflight: int
    ) -> bool:
        return self._control.acquire_gather_slot(owner, corpus, ttl, max_inflight)

    def renew_gather_slot(self, owner: str, ttl: float) -> bool:
        return self._control.renew_gather_slot(owner, ttl)

    def release_gather_slot(self, owner: str) -> None:
        self._control.release_gather_slot(owner)

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        self._control.set_gather_status(corpus, json.dumps(status, sort_keys=True))

    def list_gather_statuses(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _corpus, raw in self._control.list_gather_statuses():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def get_gather_status(self, corpus: str) -> Optional[Dict[str, Any]]:
        raw = self._control.get_gather_status(corpus)
        if raw is None:
            return None
        data: Dict[str, Any] = json.loads(raw)
        # Liveness: a non-terminal (`queued`/`running`) record with no live lease
        # is a crashed gatherer. The cloud topology shares the control plane, not
        # the cache, so the lease — held from enqueue through completion — is the
        # authoritative liveness signal.
        if (
            data.get("state") in ("queued", "running")
            and self._control.lease_holder(corpus) is None
        ):
            data["state"] = "interrupted"
        return data
