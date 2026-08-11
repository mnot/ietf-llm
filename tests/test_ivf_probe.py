"""Restricting a scan to the IVF clusters a query needs (#230).

An imported index arrives already partitioned; using it turns a 167 MiB
read per query into ~2,300 vectors. What matters is that it is used only
when it is safe to: a gathered corpus has no partition, and a query that
already carries a filter must not have chunks hidden from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List

import numpy as np
import pytest

from ietf_llm.embeddings.search import _restrict_to_probed_clusters
from ietf_llm.embeddings.storage import (
    DEFAULT_NPROBE,
    META_NPROBE,
    FLOAT32_CODEC,
    _open_db,
    load_centroids,
)

DIM = 4


def _mem_with_centroids(count: int = 6) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE centroids (id INTEGER PRIMARY KEY, vector BLOB NOT NULL)")
    for i in range(count):
        vec = np.zeros(DIM, dtype=np.float32)
        vec[i % DIM] = 1.0 - i * 0.01
        conn.execute("INSERT INTO centroids VALUES(?, ?)", (i, vec.tobytes()))
    conn.commit()
    return conn


def _query(axis: int = 0) -> Any:
    q = np.zeros(DIM, dtype=np.float32)
    q[axis] = 1.0
    return q


def test_an_index_without_a_partition_is_scanned_whole() -> None:
    """Every gathered corpus is this case, and at its size it is right."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    assert load_centroids(conn, FLOAT32_CODEC) is None
    assert _restrict_to_probed_clusters(conn, _query(), FLOAT32_CODEC, [], []) == ([], [])


def test_a_partitioned_index_is_probed() -> None:
    conn = _mem_with_centroids()
    conn.execute("INSERT INTO meta VALUES(?, '2')", (META_NPROBE,))
    clauses, args = _restrict_to_probed_clusters(conn, _query(), FLOAT32_CODEC, [], [])
    assert clauses == ["cluster_id IN (?,?)"]
    assert len(args) == 2
    # The nearest centroids on this axis are 0 and 4 (both unit-ish on dim 0).
    assert "0" in args


def test_an_existing_filter_keeps_the_exhaustive_scan() -> None:
    """Searching within one RFC is already far more selective than the
    partition, and probing on top would hide matching chunks that happen to
    sit in unprobed clusters."""
    conn = _mem_with_centroids()
    clauses, args = _restrict_to_probed_clusters(
        conn, _query(), FLOAT32_CODEC, ["file LIKE ?"], ["rfc9110.txt"]
    )
    assert clauses == ["file LIKE ?"]
    assert args == ["rfc9110.txt"]


def test_nprobe_defaults_when_meta_is_absent() -> None:
    conn = _mem_with_centroids(count=DEFAULT_NPROBE + 5)
    clauses, args = _restrict_to_probed_clusters(conn, _query(), FLOAT32_CODEC, [], [])
    assert len(args) == DEFAULT_NPROBE
    assert clauses[0].startswith("cluster_id IN (")


def test_nprobe_is_capped_by_the_partition_size() -> None:
    conn = _mem_with_centroids(count=3)
    _clauses, args = _restrict_to_probed_clusters(conn, _query(), FLOAT32_CODEC, [], [])
    assert len(args) == 3


def test_a_dimension_mismatch_falls_back_to_a_full_scan() -> None:
    """A centroid block that does not describe this index is not a reason to
    return nothing."""
    conn = _mem_with_centroids()
    wrong = np.zeros(DIM + 1, dtype=np.float32)
    wrong[0] = 1.0
    assert _restrict_to_probed_clusters(conn, wrong, FLOAT32_CODEC, [], []) == ([], [])


def test_a_pre_v11_index_still_opens(isolated_home: Path) -> None:
    """Regression: the cluster index was once created before the migration
    that adds the column, so every older index failed to open at all."""
    conn = _open_db("wg")
    conn.execute("UPDATE meta SET value='10' WHERE key='schema_version'")
    # Drop the index first: SQLite refuses to drop a column an index covers.
    conn.execute("DROP INDEX IF EXISTS chunks_cluster")
    conn.execute("ALTER TABLE chunks DROP COLUMN cluster_id")
    conn.commit()
    conn.close()

    conn = _open_db("wg")  # must not raise
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        assert "cluster_id" in cols
        idx = {r[1] for r in conn.execute("PRAGMA index_list(chunks)")}
        assert "chunks_cluster" in idx
    finally:
        conn.close()
