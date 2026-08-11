"""The on-disk format of rfc.fyi's published semantic index.

Layout of an unpacked `index.tar.gz`::

    manifest.json          build identity, model, quantisation scale, counts
    centroids.bin          the IVF partition's centroids (int8)
    clusters/NNNN.bin      one file per cluster: int8 vectors + a JSON tail
    sources.json           per-RFC sha256 of the source text the build saw

Both binary files open with the same 24-byte header — magic, format
version, dims, row count, cluster id, JSON tail length, reserved — and
the vector block follows immediately, row-major, one signed byte per
dimension. A cluster's JSON tail is columnar: every array has `n`
entries and index `i` describes the vector at row `i`.

Three things this module is deliberately strict about, because each is a
failure that otherwise stays quiet:

**Sizes are checked against the manifest.** A centroids file of the wrong
length still answers every query — against vectors that don't describe
the partition — so the size its count and dims imply is asserted rather
than assumed. rfc.fyi's own `check-site.py` learned this one first.

**`build` and `source.commit` are required.** A consumer that recovers
chunk text has to re-run the producing chunker and join on
`(rfc, off, len)`; those keys are stable only within one publication
version and against one chunker. An index that can't say which commit
built it can't be joined against safely, so we refuse it here rather
than silently mismatching a fraction of the corpus later.

**`rfc` stays a string.** It is an integer for all but two chunks in the
corpus, where it is `"17a"`. Coercing loses those; worse, coercing with a
sentinel makes them look like a lookup failure.

Publisher-side only — see the subpackage docstring.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import numpy as np

#: Magic for the two binary file kinds.
MAGIC_CENTROIDS = b"RFCV"
MAGIC_CLUSTER = b"RFCC"

#: The only format version this reader understands. rfc.fyi bumps it on an
#: incompatible layout change; a mismatch is refused rather than guessed at.
FORMAT_VERSION = 1

_HEADER_STRUCT = "<4sHHIIII"
_HEADER_SIZE = 24

#: Filenames within an unpacked index.
MANIFEST_NAME = "manifest.json"
CENTROIDS_NAME = "centroids.bin"
SOURCES_NAME = "sources.json"
CLUSTERS_DIR = "clusters"

#: The digest algorithm we accept in `sources.json`. It is named in the file
#: so the builder can change it; we check rather than assume, since a silent
#: algorithm change would turn every per-RFC comparison into a false mismatch.
_SOURCES_DIGEST = "sha256"


class RfcIndexError(Exception):
    """A published index is malformed, truncated, or an unknown version."""


@dataclass(frozen=True)
class IndexManifest:
    """The build-identifying and geometry fields we actually depend on.

    `raw` keeps the whole document, so a caller wanting a field this
    dataclass doesn't promote (cluster size histograms, the quantisation
    candidate table) can reach it without a format change here.
    """

    build: str
    built: str
    source_commit: str
    model_id: str
    dims: int
    query_prefix: str
    scale: float
    chunk_count: int
    cluster_count: int
    nprobe: int
    rfc_max: int
    raw: Dict[str, Any]


@dataclass(frozen=True)
class ChunkMeta:
    """One chunk's locator, as published. No text: the index carries byte
    ranges into the RFC's plain text, not the prose itself.

    `section` is None for the ~9% of chunks the chunker could not attribute
    to a numbered heading — mostly very old, unnumbered RFCs.
    """

    rfc: str
    off: int
    length: int
    section: Optional[str]
    title: str


@dataclass(frozen=True)
class Cluster:
    """One IVF cluster: its id, its int8 vector block, and one `ChunkMeta`
    per row of that block, in the same order.

    Clusters rather than chunks are the iteration unit because the corpus is
    ~457k chunks: handing back one object per chunk with its own vector would
    cost far more than the whole index does on disk, and every consumer wants
    the vectors as a matrix anyway.
    """

    ident: int
    vectors: "np.ndarray[Any, np.dtype[np.int8]]"
    chunks: List[ChunkMeta]


def _read_header(raw: bytes, expect_magic: bytes, where: str) -> Any:
    """Unpack and validate the shared 24-byte header. Returns
    `(dims, count, ident, meta_len)`."""
    if len(raw) < _HEADER_SIZE:
        raise RfcIndexError(f"{where}: {len(raw)} bytes, too short for a header")
    magic, version, dims, count, ident, meta_len, _reserved = struct.unpack(
        _HEADER_STRUCT, raw[:_HEADER_SIZE]
    )
    if magic != expect_magic:
        raise RfcIndexError(f"{where}: magic {magic!r}, expected {expect_magic!r}")
    if version != FORMAT_VERSION:
        raise RfcIndexError(
            f"{where}: format version {version}, this reader understands "
            f"{FORMAT_VERSION}"
        )
    return dims, count, ident, meta_len


def _require(raw: Dict[str, Any], *path: str) -> Any:
    """Fetch a nested manifest field, naming the full path when it's absent."""
    node: Any = raw
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise RfcIndexError(f"{MANIFEST_NAME}: missing {'.'.join(path)}")
        node = node[key]
    if node is None:
        raise RfcIndexError(f"{MANIFEST_NAME}: {'.'.join(path)} is null")
    return node


def read_manifest(index_dir: str) -> IndexManifest:
    """Read and validate `manifest.json` from an unpacked index."""
    path = os.path.join(index_dir, MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as err:
        raise RfcIndexError(f"{path}: unreadable ({err})") from err
    except json.JSONDecodeError as err:
        raise RfcIndexError(f"{path}: not JSON ({err})") from err
    if not isinstance(raw, dict):
        raise RfcIndexError(f"{path}: expected an object")
    version = raw.get("version")
    if version != FORMAT_VERSION:
        raise RfcIndexError(
            f"{path}: index version {version}, this reader understands "
            f"{FORMAT_VERSION}"
        )
    return IndexManifest(
        build=str(_require(raw, "build")),
        built=str(_require(raw, "built")),
        source_commit=str(_require(raw, "source", "commit")),
        model_id=str(_require(raw, "model", "id")),
        dims=int(_require(raw, "model", "dims")),
        query_prefix=str(raw.get("model", {}).get("query_prefix") or ""),
        scale=float(_require(raw, "quant", "scale")),
        chunk_count=int(_require(raw, "chunks", "count")),
        cluster_count=int(_require(raw, "clusters", "count")),
        nprobe=int(raw.get("clusters", {}).get("nprobe") or 0),
        rfc_max=int(raw.get("rfc_max") or 0),
        raw=raw,
    )


def dequantise(
    vectors: "np.ndarray[Any, np.dtype[np.int8]]", scale: float
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Int8 rows back to float32: `value * scale`.

    Rows are *not* re-normalised afterwards, matching how the index was
    written and how rfc.fyi's client scores — renormalising here would make
    our arithmetic disagree with the build's own partition assignment.
    """
    return np.asarray(vectors, dtype=np.float32) * np.float32(scale)


def read_centroids(
    index_dir: str, manifest: IndexManifest
) -> "np.ndarray[Any, np.dtype[np.int8]]":
    """The IVF centroids as an `(k, dims)` int8 matrix, size-checked."""
    path = os.path.join(index_dir, CENTROIDS_NAME)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as err:
        raise RfcIndexError(f"{path}: unreadable ({err})") from err
    dims, count, _ident, _meta_len = _read_header(raw, MAGIC_CENTROIDS, path)
    if dims != manifest.dims:
        raise RfcIndexError(f"{path}: {dims} dims, manifest says {manifest.dims}")
    if count != manifest.cluster_count:
        raise RfcIndexError(
            f"{path}: {count} centroids, manifest says {manifest.cluster_count}"
        )
    want = _HEADER_SIZE + count * dims
    if len(raw) != want:
        raise RfcIndexError(f"{path}: {len(raw)} bytes, expected {want}")
    block = np.frombuffer(raw, dtype=np.int8, count=count * dims, offset=_HEADER_SIZE)
    return block.reshape(count, dims)


def _decode_tail(tail: Dict[str, Any], count: int, where: str) -> List[ChunkMeta]:
    """Turn a cluster's columnar JSON tail into `ChunkMeta` rows."""
    if int(tail.get("n", -1)) != count:
        raise RfcIndexError(f"{where}: tail n={tail.get('n')}, header count={count}")
    strs = tail.get("str")
    if not isinstance(strs, list):
        raise RfcIndexError(f"{where}: tail has no string table")
    columns = ("rfc", "off", "len", "sec", "title")
    for name in columns:
        column = tail.get(name)
        if not isinstance(column, list) or len(column) != count:
            raise RfcIndexError(
                f"{where}: tail column {name!r} is not a list of {count}"
            )

    def _string(idx: int) -> str:
        if not 0 <= idx < len(strs):
            raise RfcIndexError(f"{where}: string index {idx} out of range")
        return str(strs[idx])

    out: List[ChunkMeta] = []
    for i in range(count):
        sec = int(tail["sec"][i])
        out.append(
            ChunkMeta(
                # An integer for all but two chunks in the corpus; see the
                # module docstring on why this stays a string.
                rfc=str(tail["rfc"][i]),
                off=int(tail["off"][i]),
                length=int(tail["len"][i]),
                section=_string(sec) if sec >= 0 else None,
                title=_string(int(tail["title"][i])),
            )
        )
    return out


def cluster_path(index_dir: str, ident: int) -> str:
    """Path of one cluster file. The manifest carries a `clusters.path`
    template, but it has been `clusters/{id:04d}.bin` since the format was
    defined and a template we don't honour is worse than one we don't read."""
    return os.path.join(index_dir, CLUSTERS_DIR, f"{ident:04d}.bin")


def read_cluster(index_dir: str, ident: int, manifest: IndexManifest) -> Cluster:
    """Read one cluster file, validating it against `manifest`."""
    path = cluster_path(index_dir, ident)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as err:
        raise RfcIndexError(f"{path}: unreadable ({err})") from err
    dims, count, file_ident, meta_len = _read_header(raw, MAGIC_CLUSTER, path)
    if dims != manifest.dims:
        raise RfcIndexError(f"{path}: {dims} dims, manifest says {manifest.dims}")
    if file_ident != ident:
        raise RfcIndexError(f"{path}: declares cluster id {file_ident}")
    want = _HEADER_SIZE + count * dims + meta_len
    if len(raw) != want:
        raise RfcIndexError(f"{path}: {len(raw)} bytes, expected {want}")
    vectors = np.frombuffer(
        raw, dtype=np.int8, count=count * dims, offset=_HEADER_SIZE
    ).reshape(count, dims)
    if count == 0:
        return Cluster(ident=ident, vectors=vectors, chunks=[])
    start = _HEADER_SIZE + count * dims
    try:
        tail = json.loads(raw[start : start + meta_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RfcIndexError(f"{path}: tail is not JSON ({err})") from err
    return Cluster(ident=ident, vectors=vectors, chunks=_decode_tail(tail, count, path))


def iter_clusters(index_dir: str, manifest: IndexManifest) -> Iterator[Cluster]:
    """Every cluster in ascending id order.

    Empty clusters are yielded too (the partition has some), so a consumer
    counting chunks against `manifest.chunk_count` sees the whole partition
    rather than a silently shorter one.
    """
    for ident in range(manifest.cluster_count):
        yield read_cluster(index_dir, ident, manifest)


def read_sources(index_dir: str) -> Dict[str, str]:
    """Per-RFC digest of the source text this build saw, keyed by RFC number.

    This sidecar is how a reissue is noticed: RFC 9920 §7.6 permits
    republishing an RFC, so `(rfc, off, len)` is stable only within one
    publication version. A consumer joining against its own text mirror
    compares these digests to tell "my mirror matches the build" from "this
    RFC moved underneath me".

    Returns an empty mapping when the sidecar is absent — it is a follow-up
    in rfc.fyi#56, so an early release legitimately won't have one.
    """
    path = os.path.join(index_dir, SOURCES_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as err:
        raise RfcIndexError(f"{path}: unreadable ({err})") from err
    except json.JSONDecodeError as err:
        raise RfcIndexError(f"{path}: not JSON ({err})") from err
    if not isinstance(raw, dict):
        raise RfcIndexError(f"{path}: expected an object")
    algorithm = raw.get("digest")
    if algorithm != _SOURCES_DIGEST:
        raise RfcIndexError(
            f"{path}: digest {algorithm!r}, this reader understands "
            f"{_SOURCES_DIGEST!r}"
        )
    rfcs = raw.get("rfcs")
    if not isinstance(rfcs, dict):
        raise RfcIndexError(f"{path}: no `rfcs` object")
    return {str(k): str(v) for k, v in rfcs.items()}


def verify(index_dir: str) -> Dict[str, Any]:
    """Read the whole index, checking it end to end, and report what's in it.

    Raises `RfcIndexError` on the first inconsistency. The returned summary is
    what a publisher should log: it is the evidence that the artifact it is
    about to re-bundle is the one the manifest describes.
    """
    manifest = read_manifest(index_dir)
    centroids = read_centroids(index_dir, manifest)
    chunks = 0
    empty = 0
    sectioned = 0
    rfcs = set()
    for cluster in iter_clusters(index_dir, manifest):
        chunks += len(cluster.chunks)
        if not cluster.chunks:
            empty += 1
        for chunk in cluster.chunks:
            rfcs.add(chunk.rfc)
            if chunk.section is not None:
                sectioned += 1
    if chunks != manifest.chunk_count:
        raise RfcIndexError(
            f"{index_dir}: {chunks} chunks across the partition, manifest says "
            f"{manifest.chunk_count}"
        )
    sources = read_sources(index_dir)
    return {
        "build": manifest.build,
        "source_commit": manifest.source_commit,
        "model": manifest.model_id,
        "dims": manifest.dims,
        "centroids": int(centroids.shape[0]),
        "clusters": manifest.cluster_count,
        "empty_clusters": empty,
        "chunks": chunks,
        "sectioned_chunks": sectioned,
        "rfcs": len(rfcs),
        "source_digests": len(sources),
    }
