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
from abc import ABC, abstractmethod
from typing import List


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
