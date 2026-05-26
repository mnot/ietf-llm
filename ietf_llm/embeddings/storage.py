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
from typing import Iterable, List, Optional, Tuple

import numpy as np

from ..utils import get_cache_dir


def _db_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "embeddings.db")


def _open_db(wg: str) -> sqlite3.Connection:
    path = _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id        INTEGER PRIMARY KEY,
            file      TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            title     TEXT NOT NULL,
            text      TEXT NOT NULL,
            embedding BLOB NOT NULL,
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
    return conn


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


def get_chunk(wg: str, file: str, chunk_idx: int) -> Optional[Tuple[str, str]]:
    """Fetch the full text of a stored chunk. Returns (title, text)."""
    if not os.path.exists(_db_path(wg)):
        return None
    conn = sqlite3.connect(_db_path(wg))
    try:
        cur = conn.execute(
            "SELECT title, text FROM chunks WHERE file=? AND chunk_idx=?",
            (file, chunk_idx),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (str(row[0]), str(row[1]))
    finally:
        conn.close()
