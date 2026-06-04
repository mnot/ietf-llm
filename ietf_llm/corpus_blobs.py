"""file:// blob store for the cloud CorpusStore backend.

A dumb, whole-object, immutable store: put / get / exists / list by key, plus
materialise-a-prefix into a local directory. Keys are POSIX-style relative
paths (e.g. `tls/<version>/digests/index.md`); the file:// backend maps a key
to `<base_dir>/<key>`. In a cloud deployment this is an S3-compatible store —
the program is the S3 client, keys map to object keys — using only whole-object
GET / PUT / HEAD / LIST, no store-special features, because all atomicity lives
in the control-plane pointer (`corpus_control`), never in the blob store. See
`docs/cloud-storage.md`.
"""

from __future__ import annotations

import os
from typing import List


def _safe_key(key: str) -> str:
    """Validate a blob key — a non-empty POSIX-relative path — and return it.

    Raises ValueError on anything that could escape the base directory: an
    absolute path, or a path with an empty, `.`, or `..` segment. This is the
    blob-store mirror of the read-side path guard.
    """
    if not key or key.startswith("/"):
        raise ValueError(f"blob key must be a non-empty relative path: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"unsafe blob key: {key!r}")
    return key


class BlobStore:
    """Whole-object immutable blob store rooted at `base_dir` (file://)."""

    def __init__(self, base_dir: str) -> None:
        self._base = base_dir

    def _path(self, key: str) -> str:
        return os.path.join(self._base, _safe_key(key))

    def put(self, key: str, data: bytes) -> None:
        """Store `data` at `key`. Written via a temp file + atomic rename so a
        concurrent reader never observes a partial object."""
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        """The bytes stored at `key`. Raises FileNotFoundError if absent."""
        with open(self._path(key), "rb") as handle:
            return handle.read()

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def list_prefix(self, prefix: str) -> List[str]:
        """Keys of every object under `prefix`, sorted.

        `prefix` is treated as a directory-aligned key prefix (how publish and
        materialise use it — e.g. `tls/<version>/`); a prefix that names no
        directory yields an empty list. The S3 backend will use a native
        string-prefix ListObjectsV2 instead.
        """
        root = os.path.join(self._base, prefix) if prefix else self._base
        if not os.path.isdir(root):
            return []
        keys: List[str] = []
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, self._base)
                keys.append(rel.replace(os.sep, "/"))
        return sorted(keys)

    def materialise_prefix(self, prefix: str, dest_dir: str) -> None:
        """Copy every object under `prefix` into `dest_dir`, stripping `prefix`
        from each key to form the destination-relative path.

        So `materialise_prefix("tls/<v>/files/", dest)` lays the corpus's
        `files/` tree out directly under `dest`. This is how the cloud read
        path stages an immutable version onto local scratch before reading it
        with the `paths.py` helpers.
        """
        src_root = os.path.join(self._base, _safe_key(prefix.rstrip("/")))
        if not os.path.isdir(src_root):
            return
        for dirpath, _dirs, files in os.walk(src_root):
            for name in files:
                src = os.path.join(dirpath, name)
                rel = os.path.relpath(src, src_root)
                dest = os.path.join(dest_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(src, "rb") as src_handle:
                    data = src_handle.read()
                with open(dest, "wb") as dest_handle:
                    dest_handle.write(data)
