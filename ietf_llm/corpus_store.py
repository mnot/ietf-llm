"""The CorpusStore seam: where a corpus's files and index live, and how a
gather is published.

This is deliberately *coarse-grained*. The store does not mediate individual
file reads or writes — those still go through `paths.py` against a local
directory. It answers two questions instead:

  - **read side** — "give me the local directory for the current version of
    corpus X" (`resolve_current` + `local_cache_dir`). Callers then read it with
    the `paths.py` helpers exactly as before.
  - **write side** — "publish this local workspace as a new version" (added in a
    later step; not yet part of the interface).

The local filesystem is the default backend and the only one the laptop CLI
uses: the live cache *is* the single version, so `resolve_current` returns a
constant sentinel and `local_cache_dir` returns the real
`~/.cache/ietf-llm/<corpus>/files`. A cloud backend (SQL control plane +
object-store blob plane) resolves a version pointer and materialises the
immutable blobs into local scratch, returning that path instead — same
interface, different backend. See `docs/storage.md`.
"""

from __future__ import annotations

import contextvars
import importlib
import os
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, cast

from . import service_config
from .utils import cached_wg_names, get_cache_dir, get_index_dir

#: Version token the local backend returns for any present corpus. The local
#: cache is single-version (the live tree on disk), so the value is opaque — it
#: only has to be non-None to mean "this corpus is present".
LOCAL_VERSION = "local"


def _local_files_dir(corpus: str) -> str:
    """The `<cache>/<corpus>/files` path under the local cache root. Pure path
    construction — does not touch the filesystem (so it never creates a dir)."""
    return os.path.join(get_cache_dir(), corpus, "files")


#: Request-scoped pinned versions: corpus -> version. Set once at the MCP tool
#: boundary so every read in a request (files *and* index) resolves the same
#: version, and a publish landing mid-request cannot mix two versions into one
#: answer. Only the cloud backend consults it; the local backend is
#: single-version. A ContextVar so it is isolated per request/worker thread.
_pinned_versions: contextvars.ContextVar[Dict[str, str]] = contextvars.ContextVar(
    "ietf_llm_pinned_versions", default={}
)


#: Process-global refcount of versions pinned by an in-flight request, across
#: all worker threads: (corpus, version) -> count. The scratch reaper consults
#: it so it never deletes a version dir a request is still reading. (A ContextVar
#: is per-context and not enumerable across threads, hence this explicit registry
#: alongside it.)
_pin_counts: Dict[Tuple[str, str], int] = {}
_pin_lock = threading.Lock()


@contextmanager
def pin_corpus_version(corpus: str, version: str) -> Iterator[None]:
    """Pin `corpus` to `version` for the duration of the block (request-scoped),
    and register it in the cross-thread in-use set the scratch reaper reads."""
    token = _pinned_versions.set({**_pinned_versions.get(), corpus: version})
    key = (corpus, version)
    with _pin_lock:
        _pin_counts[key] = _pin_counts.get(key, 0) + 1
    try:
        yield
    finally:
        _pinned_versions.reset(token)
        with _pin_lock:
            remaining = _pin_counts.get(key, 0) - 1
            if remaining > 0:
                _pin_counts[key] = remaining
            else:
                _pin_counts.pop(key, None)


def pinned_version(corpus: str) -> Optional[str]:
    """The version `corpus` is pinned to in this request, or None."""
    return _pinned_versions.get().get(corpus)


def pinned_versions_in_use(corpus: str) -> Set[str]:
    """Versions of `corpus` pinned by any in-flight request, across threads — the
    set the scratch reaper must keep so an in-progress read is never reaped."""
    with _pin_lock:
        return {ver for (cor, ver), n in _pin_counts.items() if cor == corpus and n > 0}


class CorpusStore(ABC):
    """Brokers materialisation (read) and publication (write) of a corpus.

    Backends: `LocalCorpusStore` (filesystem, single-version) today; a SQL +
    object-store backend for cloud deployments (`docs/storage.md`).
    """

    @abstractmethod
    def list_corpora(self) -> List[str]:
        """Names of every gathered corpus, sorted."""

    @abstractmethod
    def resolve_current(self, corpus: str) -> Optional[str]:
        """Current version token for `corpus`, or None if it is not present.

        Read-only — never creates anything. A non-None token identifies the
        version a request should read and can be pinned for that request's
        duration; the local backend is single-version, so the token is a
        constant sentinel.
        """

    @abstractmethod
    def local_cache_dir(self, corpus: str) -> Optional[str]:
        """Local filesystem path to the `files/` tree for the current version
        of `corpus`, or None if the corpus is not present.

        Callers read it through the `paths.py` helpers. Read-only — never
        creates the directory. The local backend returns the existing tree; a
        cloud backend materialises the version's blobs here first.
        """

    @abstractmethod
    def local_index_dir(self, corpus: str) -> Optional[str]:
        """Local filesystem directory holding the current version's
        `embeddings.db`, or None if the corpus has no current version.

        The read side of the search index resolves the DB path through this, so
        a cloud reader replica serves the *version's* index rather than an empty
        local index dir. The local backend returns its index dir directly; a
        cloud backend materialises the version (same as `local_cache_dir`) and
        returns the dir that holds `embeddings.db`. (The *write* path —
        `build_index` during a gather — does not use this; it writes the local
        index dir.)
        """

    @abstractmethod
    def publish(
        self,
        corpus: str,
        workspace: str,
        *,
        extra_files: Optional[Dict[str, str]] = None,
    ) -> str:
        """Publish the gathered tree at local `workspace` as a new version of
        `corpus`, atomically, and return its version token.

        `extra_files` maps a version-relative path to an absolute local path for
        files that live *outside* `workspace` but belong in the version — notably
        the embeddings index when `IETF_LLM_INDEX_DIR` points away from the cache
        (so a cloud reader replica still gets the version's `embeddings.db`).

        This is the write-side seam. The local backend treats it as a no-op
        finalise (the workspace already *is* the live cache, and the index is
        read from its own dir). A cloud backend uploads the workspace plus any
        `extra_files` as a fresh immutable version and flips the current-version
        pointer in one transaction, so readers see the old version or the new,
        never a half-published one.
        """

    def corpus_exists(self, corpus: str) -> bool:
        """True if `corpus` has a resolvable current version. Defined in terms
        of `resolve_current` so a backend implements only the primitive;
        read-only, creates nothing."""
        return self.resolve_current(corpus) is not None

    def materialised_cache_dir(self, corpus: str) -> Optional[str]:
        """Local `files/` dir for the current version **only if it is already
        present on local disk** — never fetches, never creates. None otherwise.

        This is the accessor for cheap discovery paths (the corpus-listing
        classification in `corpus.py`): unlike `local_cache_dir`, it must not
        trigger a per-corpus blob download when a listing classifies every
        corpus the control plane knows about. Callers degrade gracefully when
        it returns None (classify from config alone).

        The default delegates to `local_cache_dir`, correct for any backend
        whose `local_cache_dir` does not itself fetch — the local backend,
        whose `local_cache_dir` is already a read-only existence check. The
        cloud backend overrides it to consult only staged scratch."""
        return self.local_cache_dir(corpus)

    def seed_workspace(self, corpus: str, dest_root: str) -> Optional[str]:
        """Pre-populate a gather workspace at `dest_root` with the current
        published version of `corpus`, before the gather runs. Returns the
        seeded version token, or None when there is nothing to seed (no current
        version) or the backend needs no seeding.

        This is what makes an incremental gather pay off on a *fresh* host: with
        the prior version's `files/` and `embeddings.db` in place, the gather
        skips re-downloading immutable inputs (drafts, RFCs — existence-checked)
        and skips re-embedding unchanged files (the index keys on content hash,
        so identical bytes are recognised even on new local files). Without it,
        a second gather on a different replica starts cold on every axis.

        Default (local backend): a no-op returning None — the live cache under
        `dest_root` already *is* the workspace, so there is nothing to fetch.
        The cloud backend overrides this to materialise the current version's
        blobs into `dest_root` (and the index into its configured dir). It only
        reads the published version; it never publishes."""
        return None

    # --- write-side gather lease (default: no-op single writer) -----------

    def acquire_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        """Take the cross-host gather lease for `corpus` on behalf of `owner`
        for `ttl` seconds; return True if granted.

        The default always grants: a single-box backend needs no distributed
        lease — the local gather is already serialised by a file lock. A cloud
        backend overrides this with a real lease in its control plane so two
        hosts (a cron gather and the serve fleet's in-session gather) cannot
        write the same corpus at once.
        """
        return True

    def renew_lease(self, corpus: str, owner: str, ttl: float) -> bool:
        """Extend `owner`'s lease by `ttl` seconds; True if still held. Default
        no-op grants."""
        return True

    def release_lease(self, corpus: str, owner: str) -> None:
        """Release `owner`'s lease on `corpus`. Default no-op."""

    # --- fleet-wide gather concurrency cap (default: no global limit) ------

    def acquire_gather_slot(
        self, owner: str, corpus: str, ttl: float, max_inflight: int
    ) -> bool:
        """Claim one of `max_inflight` fleet-wide gather slots for `owner`;
        True if granted.

        The default always grants: a single-box backend has no fleet to bound —
        the per-host worker already serialises its own gathers. A cloud backend
        overrides this with a real global slot in its control plane so the whole
        deployment runs at most `max_inflight` gathers at once (politeness toward
        shared upstreams like datatracker)."""
        return True

    def renew_gather_slot(self, owner: str, ttl: float) -> bool:
        """Extend `owner`'s gather slot; True if still held. Default no-op
        grants."""
        return True

    def release_gather_slot(self, owner: str) -> None:
        """Release `owner`'s gather slot. Default no-op."""

    # --- fleet-visible gather status (default: backend keeps it locally) ---

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        """Record fleet-visible gather status for `corpus`. Default no-op: the
        local backend keeps status in the per-corpus `gather-status.json` (one
        host), so there is nothing to share across replicas."""

    def get_gather_status(self, corpus: str) -> Optional[Dict[str, Any]]:
        """Fleet-visible gather status, or None when the backend keeps status
        locally (the local backend — callers then fall back to the local file)."""
        return None

    def list_gather_statuses(self) -> List[Dict[str, Any]]:
        """All fleet-visible gather statuses. Default empty: the local backend
        keeps status in per-corpus files, which `gather_runner.all_statuses`
        enumerates by walking the cache."""
        return []


class LocalCorpusStore(CorpusStore):
    """Filesystem backend: the live cache under `get_cache_dir()` is the corpus,
    single-version. The laptop-CLI default; behaviour matches the direct
    `utils` / `paths.py` access it stands in front of."""

    def list_corpora(self) -> List[str]:
        return cached_wg_names()

    def resolve_current(self, corpus: str) -> Optional[str]:
        return LOCAL_VERSION if os.path.isdir(_local_files_dir(corpus)) else None

    def local_cache_dir(self, corpus: str) -> Optional[str]:
        files = _local_files_dir(corpus)
        return files if os.path.isdir(files) else None

    def local_index_dir(self, corpus: str) -> Optional[str]:
        # The live index dir — `<index_root>/<corpus>` — exactly where
        # `_db_path` has always resolved it, so local read behaviour is
        # unchanged. Returned even if no DB exists yet (search handles that).
        return os.path.join(get_index_dir(), corpus)

    def publish(
        self,
        corpus: str,
        workspace: str,
        *,
        extra_files: Optional[Dict[str, str]] = None,
    ) -> str:
        # No-op finalise: the local cache is the single live version, the gather
        # writes straight into it, and the index is read from its own dir — so
        # there is nothing to upload or flip (`extra_files` is irrelevant here).
        return LOCAL_VERSION


def get_corpus_store() -> CorpusStore:
    """The `CorpusStore` selected by service config.

    `store_backend` (env `IETF_LLM_STORE_BACKEND` > global config > `local`)
    chooses the backend: `local` (the laptop / single-box default, today's
    behaviour) or `cloud` (a SQL control plane + object-store blob plane; see
    `corpus_store_cloud`). Constructed per call — both backends are cheap,
    stateless handles that open no connection until used. An unrecognised
    backend value **raises** rather than silently falling back to local — so a
    typo (e.g. `Cloud`) on a cron/CLI gather fails loudly instead of writing to
    a local cache and never publishing to the fleet."""
    backend = service_config.store_backend()
    if backend == "local":
        return LocalCorpusStore()
    if backend == "cloud":
        # Loaded dynamically rather than with a static
        # `from .corpus_store_cloud import ...`: that module imports CorpusStore
        # from here, so a static back-import would be a cycle. The cloud
        # machinery stays out of the default local path entirely.
        cloud = importlib.import_module(f"{__package__}.corpus_store_cloud")
        return cast(CorpusStore, cloud.build_cloud_store())
    raise ValueError(
        f"unrecognised IETF_LLM_STORE_BACKEND={backend!r} (expected 'local' or 'cloud')"
    )
