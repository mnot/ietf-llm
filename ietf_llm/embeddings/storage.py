"""SQLite layer for the per-WG embedding index.

One DB per WG at ~/.cache/ietf-llm/<wg>/embeddings.db, with two tables:

  chunks(id, file, chunk_idx, title, text, embedding)
      One row per indexed chunk. The embedding column holds a packed
      float32 vector (already L2-normalised so search is a dot product).

  meta(key, value)
      Per-index metadata: the model id used to produce the vectors,
      and one `mtime:<filename>` row per indexed file for incremental
      re-embedding.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from ..utils import get_cache_dir

#: Bumped when the chunks-table schema changes. _open_db migrates older
#: databases forward via ALTER TABLE so users don't have to re-embed,
#: but newly-indexed chunks will get the richer metadata; rows from the
#: pre-migration era will have NULL in the new columns until the user
#: runs `--rebuild-embeddings`.
_SCHEMA_VERSION = 6


def _db_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "embeddings.db")


def _open_db(wg: str) -> sqlite3.Connection:
    path = _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id         INTEGER PRIMARY KEY,
            file       TEXT NOT NULL,
            chunk_idx  INTEGER NOT NULL,
            title      TEXT NOT NULL,
            text       TEXT NOT NULL,
            embedding  BLOB NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            chunk_date TEXT,              -- ISO 8601 UTC, NULL for undated chunks
            labels     TEXT,              -- comma-separated, lowercased; for issue chunks
            state      TEXT,              -- 'open'/'closed' for issue chunks; NULL elsewhere
            url        TEXT,              -- GitHub URL or IETF Archived-At; NULL elsewhere
            UNIQUE (file, chunk_idx)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a legacy DB up to the current schema in place.

    Idempotent. Fresh DBs created by `_open_db` are already at the
    current schema (the CREATE TABLE above includes every column), so
    this only does work for older DBs.
    """
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    current = int(row[0]) if row else 1
    if current >= _SCHEMA_VERSION:
        return

    cur = conn.execute("PRAGMA table_info(chunks)")
    have = {r[1] for r in cur.fetchall()}
    # v1 → v2: line tracking columns.
    if "start_line" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN start_line INTEGER")
    if "end_line" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN end_line INTEGER")
    # v2 → v3: chunk_date for faceted search.
    if "chunk_date" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN chunk_date TEXT")
    # v3 → v4: per-issue labels for faceted search.
    # Existing chunks get NULL until --rebuild-embeddings.
    if "labels" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN labels TEXT")
    # v4 → v5: per-issue state ('open'/'closed'). Lets a search filter
    # by resolution status — useful when prioritising the chairs'
    # decision over an older mid-debate thread.
    if "state" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN state TEXT")
    # v5 → v6: chunk-level citation URL. GitHub issues URL for issue
    # chunks, IETF mail archive permalink (`Archived-At:`) for thread
    # message chunks; NULL for drafts/transcripts/etc.
    if "url" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN url TEXT")

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def _pack(vec: Iterable[float]) -> bytes:
    """Pack and L2-normalise a vector for storage."""
    arr = np.asarray(list(vec), dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    return arr.tobytes()


def _unpack_matrix(rows: List[bytes]) -> np.ndarray:
    """Reshape a list of packed vectors into a single (n, dim) matrix."""
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    dim = len(rows[0]) // 4
    return np.frombuffer(b"".join(rows), dtype=np.float32).reshape(len(rows), dim)


def chunk_counts(wg: str) -> Dict[str, int]:
    """Return {filename: chunk_count} for every file in the WG's index.

    Empty dict if no index exists yet. Used by tool_list_files so the
    consumer can see how many chunk_idx values are valid for each file
    instead of having to blind-probe.
    """
    if not os.path.exists(_db_path(wg)):
        return {}
    conn = sqlite3.connect(_db_path(wg))
    try:
        cur = conn.execute(
            "SELECT file, COUNT(*) FROM chunks GROUP BY file"
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def get_chunk(
    wg: str, file: str, chunk_idx: int
) -> Optional[Tuple[str, str, Optional[int], Optional[int]]]:
    """Fetch the full text of a stored chunk.

    Returns (title, text, start_line, end_line). start_line / end_line
    are 1-indexed and inclusive when known; they may be None for chunks
    indexed before line tracking was added (schema v1).
    """
    if not os.path.exists(_db_path(wg)):
        return None
    conn = sqlite3.connect(_db_path(wg))
    try:
        cur = conn.execute(
            "SELECT title, text, start_line, end_line FROM chunks "
            "WHERE file=? AND chunk_idx=?",
            (file, chunk_idx),
        )
        row = cur.fetchone()
        if not row:
            return None
        start_line = int(row[2]) if row[2] is not None else None
        end_line = int(row[3]) if row[3] is not None else None
        return (str(row[0]), str(row[1]), start_line, end_line)
    finally:
        conn.close()
