"""Schema migration tests for the per-WG embedding index.

The v7 → v8 step is the one that needs real coverage: it can't be a plain
ALTER (the UNIQUE constraint has to widen from (file, chunk_idx) to
(file, chunk_idx, sub_idx)), so `_migrate` recreates the table. This must
preserve existing rows as sub_idx 0 and afterwards accept several sub_idx
rows for one (file, chunk_idx) — without that, a long message couldn't own
multiple fragments on a migrated DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np

from ietf_llm.embeddings.storage import _db_path, _open_db, _pack

# The exact v7 chunks-table DDL (pre-sub_idx, narrow UNIQUE constraint).
_V7_DDL = """
    CREATE TABLE chunks (
        id         INTEGER PRIMARY KEY,
        file       TEXT NOT NULL,
        chunk_idx  INTEGER NOT NULL,
        title      TEXT NOT NULL,
        text       TEXT NOT NULL,
        embedding  BLOB NOT NULL,
        start_line INTEGER,
        end_line   INTEGER,
        chunk_date TEXT,
        labels     TEXT,
        state      TEXT,
        url        TEXT,
        duplicate_of INTEGER,
        closing_rationale TEXT,
        UNIQUE (file, chunk_idx)
    )
"""


def _make_v7_db(wg: str) -> None:
    path = _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_V7_DDL)
    conn.execute(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '7')")
    conn.execute("INSERT INTO meta(key, value) VALUES('model', 'stub')")
    vec = _pack(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    conn.execute(
        "INSERT INTO chunks (file, chunk_idx, title, text, embedding, "
        "start_line, end_line, chunk_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("threads/t.md", 1, "[1] Alice", "old body", vec, 5, 9, "2025-01-01T10:00:00Z"),
    )
    conn.commit()
    conn.close()


def test_v8_to_v9_backfills_chunk_hash(isolated_home: Path) -> None:
    from ietf_llm.embeddings.storage import chunk_hash
    _make_v7_db("wg")  # migrates all the way to v9

    conn = _open_db("wg")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        assert "chunk_hash" in cols
        # A non-windowed chunk's embedded text is exactly its stored text, so the
        # migration backfills the hash without re-embedding.
        h = conn.execute(
            "SELECT chunk_hash FROM chunks WHERE file='threads/t.md'"
        ).fetchone()[0]
        assert h == chunk_hash("old body")
    finally:
        conn.close()


def test_v7_to_v8_preserves_rows_and_widens_unique(isolated_home: Path) -> None:
    _make_v7_db("wg")

    conn = _open_db("wg")  # runs _migrate
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        assert "sub_idx" in cols

        # schema_version bumped.
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        assert row[0] == "9"

        # The existing row survived, carried forward as sub_idx 0 with all
        # its metadata intact.
        r = conn.execute(
            "SELECT chunk_idx, sub_idx, title, text, start_line, end_line, "
            "chunk_date FROM chunks WHERE file='threads/t.md'"
        ).fetchone()
        assert r == (1, 0, "[1] Alice", "old body", 5, 9, "2025-01-01T10:00:00Z")

        # The widened UNIQUE constraint now accepts a second fragment for the
        # same (file, chunk_idx) — impossible under the old (file, chunk_idx)
        # constraint.
        vec = _pack(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
        conn.execute(
            "INSERT INTO chunks (file, chunk_idx, sub_idx, title, text, "
            "embedding) VALUES (?, ?, ?, ?, ?, ?)",
            ("threads/t.md", 1, 1, "[1] Alice (part 2/2)", "tail", vec),
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE file='threads/t.md' AND chunk_idx=1"
        ).fetchone()[0]
        assert n == 2
    finally:
        conn.close()
