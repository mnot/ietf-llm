"""The cloud CorpusStore backend: a control plane + a blob plane composed.

`CloudCorpusStore` is the CorpusStore implementation a cloud deployment uses.
It holds no storage logic of its own — it composes a `ControlPlane`
(transactional versions / pointer / leases) and a `BlobStore` (immutable
whole-object blobs), and stages materialised versions onto a local scratch
directory. The current pieces are SQLite + `file://`; swapping in Postgres +
S3 means swapping those two injected components, not this class. See
`docs/cloud-storage.md`.

Publish ordering is the load-bearing invariant: blobs go to a *fresh* version
prefix first (invisible — nothing references it), then the control plane records
the version and flips the pointer in one transaction. An interruption before
that flip leaves the prior version current and the staged blobs orphaned, never
a torn read.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from .corpus_blobs import BlobStore
from .corpus_control import ControlPlane
from .corpus_store import CorpusStore


def _new_version() -> str:
    """A unique, lexically-sortable version token: a UTC timestamp plus a short
    random suffix so two publishes in the same second do not collide."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class CloudCorpusStore(CorpusStore):
    """CorpusStore over a transactional control plane + an immutable blob store,
    materialising versions onto `scratch_dir`."""

    def __init__(
        self, control: ControlPlane, blobs: BlobStore, scratch_dir: str
    ) -> None:
        self._control = control
        self._blobs = blobs
        self._scratch = scratch_dir

    def list_corpora(self) -> List[str]:
        return self._control.list_corpora()

    def resolve_current(self, corpus: str) -> Optional[str]:
        return self._control.resolve_current(corpus)

    def local_cache_dir(self, corpus: str) -> Optional[str]:
        version = self._control.resolve_current(corpus)
        if version is None:
            return None
        dest_root = os.path.join(self._scratch, corpus, version)
        # Versions are immutable, so a materialised copy is reusable: only fetch
        # if this version is not already staged locally.
        if not os.path.isdir(dest_root):
            self._blobs.materialise_prefix(f"{corpus}/{version}/", dest_root)
        files_dir = os.path.join(dest_root, "files")
        return files_dir if os.path.isdir(files_dir) else None

    def publish(
        self, corpus: str, workspace: str, version: Optional[str] = None
    ) -> str:
        version = version or _new_version()
        prefix = f"{corpus}/{version}/"
        files: List[str] = []
        # Stage every file in the workspace to the fresh version prefix. Nothing
        # references this prefix yet, so a partial upload is invisible.
        for dirpath, _dirs, names in os.walk(workspace):
            for name in names:
                abs_path = os.path.join(dirpath, name)
                rel = os.path.relpath(abs_path, workspace).replace(os.sep, "/")
                with open(abs_path, "rb") as handle:
                    self._blobs.put(prefix + rel, handle.read())
                files.append(rel)
        # Atomically record the version and flip the pointer. If this raises,
        # the staged blobs are orphaned and the prior version stays current.
        self._control.publish_version(
            corpus, version, {"version": version, "files": sorted(files)}
        )
        return version
