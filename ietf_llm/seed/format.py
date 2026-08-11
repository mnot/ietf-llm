"""Seed-store on-disk format: the JSON index/manifest schema, bundle assembly,
integrity hashing, and safe extraction shared by the publisher
(`scripts/publish_seeds.py`) and the consumer fetch path (`seed.fetch`).

Deliberately dependency-light — **stdlib only** (`sqlite3`, `tarfile`,
`hashlib`, `json`), no network, no torch, no gather imports — so both the
producer script and the consumer can import it cheaply and it stays trivially
testable. Callers pass in resolved paths and values; this module owns the format,
not where files live (that is `paths.py`'s job). See `docs/seed-store.md`.

Layout on the static host::

    index.json                              root index (+ compatibility tuple)
    <name>/manifest.json                    one per corpus (self-describing)
    <name>/<name>-<version>.tar.gz          one gzipped-tar bundle per corpus

A bundle's arcnames are version-relative paths a consumer installs into
``<cache>/<corpus>/``: ``files/…`` (minus ``files/raw/``), the incremental-gather
manifests, and the index files (``embeddings.db``, ``topics.json``) at the top
level even when ``IETF_LLM_INDEX_DIR`` splits them onto a separate volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

#: Bumped only on an incompatible change to the JSON schema below. A consumer
#: refuses an index whose `format` it does not understand.
FORMAT_VERSION = 1

#: The root index filename at the store root.
INDEX_NAME = "index.json"

#: The index files that live in `IETF_LLM_INDEX_DIR/<corpus>/` rather than the
#: corpus cache dir, and so are added to a bundle explicitly (top-level arcnames)
#: rather than picked up by the corpus-dir walk.
INDEX_FILES: Tuple[str, ...] = ("embeddings.db", "topics.json")

#: Streaming read size for hashing / copying.
_CHUNK = 1 << 20


class SeedFormatError(Exception):
    """A seed bundle, manifest, or index is malformed or incompatible."""


# --------------------------------------------------------------------------- #
# Compatibility tuple (read from an embeddings.db `meta` table)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CompatTuple:
    """The vector-compatibility gate: an index and a consumer must agree on all
    four before the consumer can use the shipped vectors. Read straight from the
    producer's `embeddings.db` `meta` (`schema_version`, `model`,
    `chunker_version`, `embed_dim`)."""

    schema_version: int
    embedding_model: str
    chunker_version: str
    vector_dim: Optional[int]

    def matches(self, other: "CompatTuple") -> bool:
        """True if `other` (typically the consumer's own model) can use vectors
        built under this tuple. All four fields must be equal; a `None`
        vector_dim on either side is treated as unknown and does not veto (older
        indexes predate the recorded dimension)."""
        if (
            self.schema_version != other.schema_version
            or self.embedding_model != other.embedding_model
            or self.chunker_version != other.chunker_version
        ):
            return False
        if self.vector_dim is None or other.vector_dim is None:
            return True
        return self.vector_dim == other.vector_dim

    def describe_mismatch(self, other: "CompatTuple") -> str:
        """Human summary of which field(s) make `other` incompatible with this
        tuple — `field a vs b` per differing field, joined by commas. Uses the
        same fields (and the same `None`-dim leniency) as `matches`, so the
        summary is empty exactly when `matches` is True. Lets a skip/log message
        name the real culprit (usually `schema_version`) instead of printing a
        field that happens to agree."""
        diffs = []
        if self.schema_version != other.schema_version:
            diffs.append(
                f"schema_version {self.schema_version} vs {other.schema_version}"
            )
        if self.embedding_model != other.embedding_model:
            diffs.append(f"model {self.embedding_model} vs {other.embedding_model}")
        if self.chunker_version != other.chunker_version:
            diffs.append(
                f"chunker_version {self.chunker_version} vs {other.chunker_version}"
            )
        if (
            self.vector_dim is not None
            and other.vector_dim is not None
            and self.vector_dim != other.vector_dim
        ):
            diffs.append(f"vector_dim {self.vector_dim} vs {other.vector_dim}")
        return ", ".join(diffs)


def read_compat_tuple(db_path: str) -> CompatTuple:
    """Read the compatibility tuple from an `embeddings.db` at `db_path`.

    Opens the DB read-only. Raises `SeedFormatError` if the file is missing, not
    a database, or lacks the required `model` / `schema_version` /
    `chunker_version` meta keys (`embed_dim` is optional)."""
    if not os.path.isfile(db_path):
        raise SeedFormatError(f"no embeddings index at {db_path}")
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as err:
        raise SeedFormatError(f"cannot read meta from {db_path}: {err}") from err
    meta: Dict[str, str] = {str(k): str(v) for k, v in rows}
    missing = [
        k for k in ("model", "schema_version", "chunker_version") if k not in meta
    ]
    if missing:
        raise SeedFormatError(f"{db_path} is missing meta key(s): {', '.join(missing)}")
    try:
        schema_version = int(meta["schema_version"])
    except ValueError as err:
        raise SeedFormatError(f"{db_path} has non-integer schema_version") from err
    vector_dim: Optional[int] = None
    if meta.get("embed_dim"):
        try:
            vector_dim = int(meta["embed_dim"])
        except ValueError as err:
            raise SeedFormatError(f"{db_path} has non-integer embed_dim") from err
    return CompatTuple(
        schema_version=schema_version,
        embedding_model=meta["model"],
        chunker_version=meta["chunker_version"],
        vector_dim=vector_dim,
    )


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def manifest_relpath(name: str) -> str:
    """Store-relative path of a corpus's manifest."""
    return f"{name}/manifest.json"


def bundle_relpath(name: str, version: str) -> str:
    """Store-relative path of a corpus's bundle for `version`."""
    return f"{name}/{name}-{version}.tar.gz"


#: strftime pattern for a version token — a compact, lexically-sortable UTC
#: stamp of the gather it was built from.
_VERSION_STAMP = "%Y%m%dT%H%M%SZ"


def version_stamp(gathered: datetime) -> str:
    """The version token for a bundle built from a gather at `gathered` — a
    compact UTC stamp (`20260701T000000Z`). Used both as the `version` field and
    in the bundle filename. Naive datetimes are assumed UTC."""
    when = gathered if gathered.tzinfo else gathered.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime(_VERSION_STAMP)


# --------------------------------------------------------------------------- #
# Manifest and index (JSON schema)
# --------------------------------------------------------------------------- #


@dataclass
class Manifest:
    """One corpus's `<name>/manifest.json`: self-describing (repeats the
    compatibility tuple) and pointing at its bundle with an integrity hash."""

    name: str
    version: str
    compat: CompatTuple
    window_months: Optional[int]
    gathered: str
    bundle: str
    bundle_sha256: str
    bundle_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "schema_version": self.compat.schema_version,
            "embedding_model": self.compat.embedding_model,
            "chunker_version": self.compat.chunker_version,
            "vector_dim": self.compat.vector_dim,
            "window_months": self.window_months,
            "gathered": self.gathered,
            "bundle": self.bundle,
            "bundle_sha256": self.bundle_sha256,
            "bundle_bytes": self.bundle_bytes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Manifest":
        try:
            return cls(
                name=str(data["name"]),
                version=str(data["version"]),
                compat=_compat_from_dict(data),
                window_months=_opt_int(data.get("window_months")),
                gathered=str(data["gathered"]),
                bundle=str(data["bundle"]),
                bundle_sha256=str(data["bundle_sha256"]),
                bundle_bytes=int(data["bundle_bytes"]),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SeedFormatError(f"malformed manifest: {err}") from err


@dataclass
class IndexEntry:
    """One corpus's row in the root index — enough to list coverage and locate
    the manifest without fetching a bundle."""

    name: str
    kind: str
    subject: str
    window_months: Optional[int]
    gathered: str
    version: str
    manifest: str
    bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "subject": self.subject,
            "window_months": self.window_months,
            "gathered": self.gathered,
            "version": self.version,
            "manifest": self.manifest,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IndexEntry":
        try:
            return cls(
                name=str(data["name"]),
                kind=str(data.get("kind", "")),
                subject=str(data.get("subject", "")),
                window_months=_opt_int(data.get("window_months")),
                gathered=str(data.get("gathered", "")),
                version=str(data["version"]),
                manifest=str(data["manifest"]),
                bytes=int(data.get("bytes", 0)),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SeedFormatError(f"malformed index entry: {err}") from err


@dataclass
class Index:
    """The root `index.json`: the store-wide compatibility tuple plus one entry
    per covered corpus. The single source of truth for what a consumer can pull;
    every corpus shares the one tuple (one embedding model per store)."""

    generated: str
    compat: CompatTuple
    corpora: List[IndexEntry]
    format: int = FORMAT_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "generated": self.generated,
            "schema_version": self.compat.schema_version,
            "embedding_model": self.compat.embedding_model,
            "chunker_version": self.compat.chunker_version,
            "vector_dim": self.compat.vector_dim,
            "corpora": [e.to_dict() for e in self.corpora],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"

    def entry(self, name: str) -> Optional[IndexEntry]:
        for candidate in self.corpora:
            if candidate.name == name:
                return candidate
        return None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Index":
        try:
            fmt = int(data.get("format", 0))
        except (TypeError, ValueError) as err:
            raise SeedFormatError(f"malformed index: {err}") from err
        if fmt != FORMAT_VERSION:
            raise SeedFormatError(
                f"unsupported seed index format {fmt} (this build understands "
                f"{FORMAT_VERSION})"
            )
        try:
            corpora = [IndexEntry.from_dict(e) for e in data.get("corpora", [])]
            return cls(
                generated=str(data.get("generated", "")),
                compat=_compat_from_dict(data),
                corpora=corpora,
                format=fmt,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SeedFormatError(f"malformed index: {err}") from err

    @classmethod
    def from_json(cls, text: str) -> "Index":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise SeedFormatError(f"index is not valid JSON: {err}") from err
        if not isinstance(data, dict):
            raise SeedFormatError("index is not a JSON object")
        return cls.from_dict(data)


def manifest_to_json(manifest: Manifest) -> str:
    return json.dumps(manifest.to_dict(), indent=2, sort_keys=False) + "\n"


def manifest_from_json(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise SeedFormatError(f"manifest is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise SeedFormatError("manifest is not a JSON object")
    return Manifest.from_dict(data)


def _compat_from_dict(data: Dict[str, Any]) -> CompatTuple:
    try:
        return CompatTuple(
            schema_version=int(data["schema_version"]),
            embedding_model=str(data["embedding_model"]),
            chunker_version=str(data["chunker_version"]),
            vector_dim=_opt_int(data.get("vector_dim")),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise SeedFormatError(f"malformed compatibility tuple: {err}") from err


def _opt_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


# --------------------------------------------------------------------------- #
# Bundle assembly (producer) and extraction (consumer)
# --------------------------------------------------------------------------- #


def iter_bundle_members(corpus_dir: str, index_dir: str) -> List[Tuple[str, str]]:
    """`(arcname, absolute path)` for every file that belongs in a seed bundle,
    sorted by arcname. `arcname` is the version-relative path a consumer installs
    into `<cache>/<corpus>/`.

    Excludes the `files/raw/` subtree (not indexed; grep/NotebookLM only) and
    producer-local sidecars (`gather-metrics.json`, the `last-accessed` /
    `.live-cache.json` read-path state, any `.building` scratch DB). The index
    files (`embeddings.db`, `topics.json`) are added from `index_dir` at the top
    level, so a split `IETF_LLM_INDEX_DIR` still lands them in the bundle."""
    members: Dict[str, str] = {}
    for root, _dirs, names in os.walk(corpus_dir):
        for name in names:
            abs_path = os.path.join(root, name)
            arc = os.path.relpath(abs_path, corpus_dir).replace(os.sep, "/")
            if _excluded_from_bundle(arc):
                continue
            members[arc] = abs_path
    for name in INDEX_FILES:
        abs_path = os.path.join(index_dir, name)
        if os.path.isfile(abs_path):
            members[name] = abs_path
    return sorted(members.items())


def iter_index_members(index_dir: str) -> List[Tuple[str, str]]:
    """`(arcname, absolute path)` for an index-only bundle — no `files/` tree.

    For a member assembled from an upstream artifact rather than gathered
    (issue #230): there is no corpus directory to walk, because the text lives
    in the index itself. `docs/seed-store.md` lists embeddings-only
    distribution as a non-goal, on the grounds that the per-file gather skip
    makes the saving unreliable; that reasoning is about gathering, and this
    member is never gathered.
    """
    members: Dict[str, str] = {}
    for name in INDEX_FILES:
        abs_path = os.path.join(index_dir, name)
        if os.path.isfile(abs_path):
            members[name] = abs_path
    return sorted(members.items())


def _excluded_from_bundle(arc: str) -> bool:
    if arc.startswith("files/raw/") or arc == "files/raw":
        return True
    if arc in (
        "gather-metrics.json",
        "last-accessed",
        ".live-cache.json",
        "seed-source",  # a producer that itself seeds must not ship its provenance
    ):
        return True
    # Index files come from index_dir explicitly (below); a scratch build DB is
    # never shipped.
    if arc in INDEX_FILES or arc.endswith(".building"):
        return True
    return False


def build_bundle(members: List[Tuple[str, str]], dest_path: str) -> Tuple[str, int]:
    """Write `members` as a gzipped tar at `dest_path` (atomically: temp +
    `os.replace`) and return `(sha256, byte_size)` of the finished file.

    `recursive=False` on each add so only the enumerated files land — directory
    entries are not added, and nothing outside `members` sneaks in."""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp = f"{dest_path}.tmp.{os.getpid()}"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for arc, abs_path in members:
                tar.add(abs_path, arcname=arc, recursive=False)
        digest = sha256_file(tmp)
        size = os.path.getsize(tmp)
        os.replace(tmp, dest_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return digest, size


def sha256_file(path: str) -> str:
    """Streaming SHA-256 hex digest of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def verify_sha256(path: str, expected: str) -> None:
    """Raise `SeedFormatError` unless `path` hashes to `expected`."""
    actual = sha256_file(path)
    if actual != expected:
        raise SeedFormatError(
            f"bundle integrity check failed for {path}: expected {expected}, "
            f"got {actual}"
        )


def extract_bundle(bundle_path: str, dest_dir: str) -> None:
    """Extract a seed bundle into `dest_dir`, refusing any member that would
    escape it or is not a regular file/dir (no symlinks, hardlinks, or device
    nodes — a seed bundle only ever carries corpus files).

    Written to be safe on Python 3.10–3.14 without relying on the 3.12+
    extraction `filter`, so the guard is explicit here."""
    os.makedirs(dest_dir, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                raise SeedFormatError(
                    f"bundle {bundle_path} has a non-regular member: {member.name}"
                )
            target = os.path.realpath(os.path.join(dest_dir, member.name))
            if target != dest_real and not target.startswith(dest_real + os.sep):
                raise SeedFormatError(
                    f"bundle {bundle_path} member escapes destination: "
                    f"{member.name}"
                )
            # Extract member-by-member (not extractall) so this stays portable
            # across 3.10–3.14 without the 3.12+ extraction `filter`, and never
            # honours a symlink/device member (already rejected above).
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover — isfile() guaranteed above
                continue
            with source, open(target, "wb") as out:
                while True:
                    block = source.read(_CHUNK)
                    if not block:
                        break
                    out.write(block)
