"""The blob plane for the cloud CorpusStore backend: a dumb, whole-object,
immutable store of the versioned corpus bytes (`files/` + `embeddings.db`).

`BlobStore` is the interface. `FileBlobStore` is the bundled `file://` backend
(a base directory — works for development or over a shared volume); an
object-store backend (S3-compatible) can plug in behind the same interface. Keys
are POSIX-style relative paths (e.g. `tls/<version>/digests/index.md`). The store
uses only whole-object operations and no engine-special features, because all
atomicity lives in the control-plane pointer, never here. See `docs/storage.md`.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, TypeVar

_T = TypeVar("_T")


def blob_concurrency() -> int:
    """Worker count for the parallel publish / materialise paths — and the
    boto3 connection-pool size, kept in step so workers never block waiting
    for a connection. A version is hundreds-to-thousands of small objects, so
    a per-object round-trip serialised is minutes against a remote bucket;
    fanning out hides that latency. Override with `IETF_LLM_S3_CONCURRENCY`."""
    try:
        return max(1, int(os.environ.get("IETF_LLM_S3_CONCURRENCY", "16")))
    except ValueError:
        return 16


def parallel_each(fn: Callable[[_T], None], items: List[_T]) -> None:
    """Apply `fn` to every item concurrently, re-raising the first worker
    exception. Publish and materialise both rely on that: a failed upload must
    not let the pointer flip over a partial version, and a lost blob must fail
    loudly rather than drop a file from the materialised tree. Serial for a
    single item or when concurrency is disabled."""
    workers = blob_concurrency()
    if len(items) <= 1 or workers <= 1:
        for item in items:
            fn(item)
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        # Consume the lazy map so the first worker exception surfaces here.
        for _ in pool.map(fn, items):
            pass


def _safe_key(key: str) -> str:
    """Validate a blob key — a non-empty POSIX-relative path — and return it.

    Raises ValueError on anything that could escape a base directory: an
    absolute path, or a path with an empty, `.`, or `..` segment.
    """
    if not key or key.startswith("/"):
        raise ValueError(f"blob key must be a non-empty relative path: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"unsafe blob key: {key!r}")
    return key


class BlobStore(ABC):
    """Whole-object, immutable blob store keyed by POSIX-relative path."""

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Store `data` at `key` (atomically, from a reader's point of view)."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """The bytes stored at `key`; raises if absent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """True if an object is stored at `key`."""

    @abstractmethod
    def list_prefix(self, prefix: str) -> List[str]:
        """Keys of every object under `prefix`, sorted."""

    @abstractmethod
    def materialise_prefix(self, prefix: str, dest_dir: str) -> None:
        """Copy every object under `prefix` into `dest_dir`, stripping `prefix`
        from each key to form the destination-relative path."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every object under `prefix`. Used to reap a superseded version's
        whole content tree (and any failed-publish orphan prefix). A no-op when
        nothing is stored under `prefix`."""


class FileBlobStore(BlobStore):
    """Bundled `file://` backend rooted at `base_dir`."""

    def __init__(self, base_dir: str) -> None:
        self._base = base_dir

    def _path(self, key: str) -> str:
        return os.path.join(self._base, _safe_key(key))

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as handle:
            return handle.read()

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def list_prefix(self, prefix: str) -> List[str]:
        root = os.path.join(self._base, prefix) if prefix else self._base
        if not os.path.isdir(root):
            return []
        keys: List[str] = []
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), self._base)
                keys.append(rel.replace(os.sep, "/"))
        return sorted(keys)

    def materialise_prefix(self, prefix: str, dest_dir: str) -> None:
        src_root = os.path.join(self._base, _safe_key(prefix.rstrip("/")))
        if not os.path.isdir(src_root):
            return
        for dirpath, _dirs, files in os.walk(src_root):
            for name in files:
                src = os.path.join(dirpath, name)
                dest = os.path.join(dest_dir, os.path.relpath(src, src_root))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(src, "rb") as src_handle:
                    data = src_handle.read()
                with open(dest, "wb") as dest_handle:
                    dest_handle.write(data)

    def delete_prefix(self, prefix: str) -> None:
        root = os.path.join(self._base, _safe_key(prefix.rstrip("/")))
        if os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
