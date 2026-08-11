"""Unit tests for the reader of rfc.fyi's published semantic index (#230).

Fixtures are synthesised here rather than pulled from a real index: the
published artifact is ~200 MB and lives outside the repo, and every failure
mode worth testing (a truncated centroids file, a tail column of the wrong
length, a missing build id) is one a healthy index by definition doesn't have.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pytest

from ietf_llm.rfcindex import format as fmt

DIMS = 4


def _header(magic: bytes, count: int, ident: int, meta_len: int) -> bytes:
    return struct.pack("<4sHHIIII", magic, 1, DIMS, count, ident, meta_len, 0)


def _manifest(**over: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "version": 1,
        "build": "20260811T003915Z",
        "built": "2026-08-11T00:39:15Z",
        "source": {"commit": "d640e5242204d02cf8587c3e27e59856b443068d"},
        "model": {"id": "Xenova/bge-small-en-v1.5", "dims": DIMS,
                  "query_prefix": "Represent this sentence: "},
        "quant": {"scale": 0.5},
        "chunks": {"count": 3},
        "clusters": {"count": 2, "nprobe": 20},
        "rfc_max": 9999,
    }
    doc.update(over)
    return doc


def _cluster_bytes(
    ident: int, rows: List[Tuple[str, int, int, Optional[str], str]]
) -> bytes:
    """Build one cluster file from `(rfc, off, len, section, title)` rows."""
    strs: List[str] = []

    def _intern(value: str) -> int:
        if value not in strs:
            strs.append(value)
        return strs.index(value)

    tail: Dict[str, Any] = {
        "n": len(rows),
        "rfc": [r[0] for r in rows],
        "off": [r[1] for r in rows],
        "len": [r[2] for r in rows],
        "sec": [_intern(r[3]) if r[3] is not None else -1 for r in rows],
        "title": [_intern(r[4]) for r in rows],
    }
    tail["str"] = strs
    body = json.dumps(tail).encode("utf-8")
    vectors = np.arange(len(rows) * DIMS, dtype=np.int8).tobytes()
    return _header(fmt.MAGIC_CLUSTER, len(rows), ident, len(body)) + vectors + body


def _write_index(root: str, manifest: Optional[Dict[str, Any]] = None) -> str:
    """A complete two-cluster index: three chunks, one without a section."""
    doc = manifest if manifest is not None else _manifest()
    os.makedirs(os.path.join(root, fmt.CLUSTERS_DIR), exist_ok=True)
    with open(os.path.join(root, fmt.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    count = int(doc["clusters"]["count"])
    centroids = np.arange(count * DIMS, dtype=np.int8).tobytes()
    with open(os.path.join(root, fmt.CENTROIDS_NAME), "wb") as fh:
        fh.write(_header(fmt.MAGIC_CENTROIDS, count, 0, 0) + centroids)
    with open(fmt.cluster_path(root, 0), "wb") as fh:
        fh.write(
            _cluster_bytes(
                0,
                [
                    ("9110", 48213, 1180, "7.2", "Message Routing"),
                    ("17a", 10, 30, None, "A Nameless Section"),
                ],
            )
        )
    with open(fmt.cluster_path(root, 1), "wb") as fh:
        fh.write(_cluster_bytes(1, [("9111", 900, 400, "3", "Storing Responses")]))
    with open(os.path.join(root, fmt.SOURCES_NAME), "w", encoding="utf-8") as fh:
        json.dump({"digest": "sha256", "rfcs": {"9110": "aa", "9111": "bb"}}, fh)
    return root


def test_reads_a_whole_index(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    summary = fmt.verify(root)
    assert summary["chunks"] == 3
    assert summary["clusters"] == 2
    assert summary["centroids"] == 2
    assert summary["sectioned_chunks"] == 2
    assert summary["rfcs"] == 3
    assert summary["source_digests"] == 2
    assert summary["build"] == "20260811T003915Z"


def test_chunk_metadata_round_trips(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    manifest = fmt.read_manifest(root)
    clusters = list(fmt.iter_clusters(root, manifest))
    first = clusters[0].chunks[0]
    assert (first.rfc, first.off, first.length) == ("9110", 48213, 1180)
    assert (first.section, first.title) == ("7.2", "Message Routing")
    # The non-numeric RFC id survives as itself, and its absent section is None.
    odd = clusters[0].chunks[1]
    assert odd.rfc == "17a"
    assert odd.section is None
    assert clusters[0].vectors.shape == (2, DIMS)


def test_dequantise_scales_without_renormalising() -> None:
    vectors = np.array([[2, 0, 0, 0]], dtype=np.int8)
    out = fmt.dequantise(vectors, 0.5)
    assert out.dtype == np.float32
    assert out.tolist() == [[1.0, 0.0, 0.0, 0.0]]


def test_truncated_centroids_are_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    path = os.path.join(root, fmt.CENTROIDS_NAME)
    with open(path, "rb") as fh:
        raw = fh.read()
    with open(path, "wb") as fh:
        fh.write(raw[:-1])
    with pytest.raises(fmt.RfcIndexError, match="bytes, expected"):
        fmt.verify(root)


def test_centroid_count_must_match_the_manifest(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    with open(os.path.join(root, fmt.CENTROIDS_NAME), "wb") as fh:
        fh.write(_header(fmt.MAGIC_CENTROIDS, 3, 0, 0) + b"\0" * (3 * DIMS))
    with pytest.raises(fmt.RfcIndexError, match="centroids, manifest says"):
        fmt.verify(root)


def test_chunk_total_must_match_the_manifest(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path), _manifest(chunks={"count": 99}))
    with pytest.raises(fmt.RfcIndexError, match="manifest says 99"):
        fmt.verify(root)


def test_missing_build_id_is_refused(tmp_path: Any) -> None:
    doc = _manifest()
    del doc["build"]
    root = _write_index(str(tmp_path), doc)
    with pytest.raises(fmt.RfcIndexError, match="missing build"):
        fmt.read_manifest(root)


def test_missing_source_commit_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path), _manifest(source={}))
    with pytest.raises(fmt.RfcIndexError, match="missing source.commit"):
        fmt.read_manifest(root)


def test_unknown_format_version_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path), _manifest(version=2))
    with pytest.raises(fmt.RfcIndexError, match="index version 2"):
        fmt.read_manifest(root)


def test_short_tail_column_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    tail = json.dumps(
        {"n": 1, "rfc": ["9111"], "off": [1], "len": [1], "sec": [-1],
         "title": [], "str": []}
    ).encode("utf-8")
    with open(fmt.cluster_path(root, 1), "wb") as fh:
        fh.write(_header(fmt.MAGIC_CLUSTER, 1, 1, len(tail)))
        fh.write(np.zeros(DIMS, dtype=np.int8).tobytes() + tail)
    with pytest.raises(fmt.RfcIndexError, match="column 'title'"):
        fmt.verify(root)


def test_string_index_out_of_range_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    tail = json.dumps(
        {"n": 1, "rfc": ["9111"], "off": [1], "len": [1], "sec": [-1],
         "title": [7], "str": ["only"]}
    ).encode("utf-8")
    with open(fmt.cluster_path(root, 1), "wb") as fh:
        fh.write(_header(fmt.MAGIC_CLUSTER, 1, 1, len(tail)))
        fh.write(np.zeros(DIMS, dtype=np.int8).tobytes() + tail)
    with pytest.raises(fmt.RfcIndexError, match="string index 7"):
        fmt.verify(root)


def test_wrong_magic_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    with open(fmt.cluster_path(root, 1), "wb") as fh:
        fh.write(_header(fmt.MAGIC_CENTROIDS, 0, 1, 0))
    with pytest.raises(fmt.RfcIndexError, match="magic"):
        fmt.verify(root)


def test_absent_sources_sidecar_is_not_an_error(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    os.remove(os.path.join(root, fmt.SOURCES_NAME))
    assert fmt.read_sources(root) == {}
    assert fmt.verify(root)["source_digests"] == 0


def test_unknown_digest_algorithm_is_refused(tmp_path: Any) -> None:
    root = _write_index(str(tmp_path))
    with open(os.path.join(root, fmt.SOURCES_NAME), "w", encoding="utf-8") as fh:
        json.dump({"digest": "md5", "rfcs": {}}, fh)
    with pytest.raises(fmt.RfcIndexError, match="digest 'md5'"):
        fmt.read_sources(root)
