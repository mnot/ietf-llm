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
interface, different backend. See `docs/cloud-storage.md`.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import List, Optional

from .utils import cached_wg_names, get_cache_dir

#: Version token the local backend returns for any present corpus. The local
#: cache is single-version (the live tree on disk), so the value is opaque — it
#: only has to be non-None to mean "this corpus is present".
LOCAL_VERSION = "local"


def _local_files_dir(corpus: str) -> str:
    """The `<cache>/<corpus>/files` path under the local cache root. Pure path
    construction — does not touch the filesystem (so it never creates a dir)."""
    return os.path.join(get_cache_dir(), corpus, "files")


class CorpusStore(ABC):
    """Brokers materialisation (read) and publication (write) of a corpus.

    Backends: `LocalCorpusStore` (filesystem, single-version) today; a SQL +
    object-store backend for cloud deployments (`docs/cloud-storage.md`).
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

    def corpus_exists(self, corpus: str) -> bool:
        """True if `corpus` has a resolvable current version. Defined in terms
        of `resolve_current` so a backend implements only the primitive;
        read-only, creates nothing."""
        return self.resolve_current(corpus) is not None


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


@lru_cache(maxsize=1)
def get_corpus_store() -> CorpusStore:
    """The process-wide `CorpusStore`. The local filesystem backend today; a
    future service-config selection (`docs/cloud-storage.md`) will choose a
    cloud backend here. Cached so a backend that holds connections is built
    once — `get_corpus_store.cache_clear()` resets it (used by tests)."""
    return LocalCorpusStore()
