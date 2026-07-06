"""R5: embedding provenance. build_index records the embedding dimension
in the index `meta` table, and a dimension change -- a backend emitting a
different-width vector under the same model id -- forces a rebuild rather
than corrupting the packed matrix with mixed widths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.embeddings.storage import _db_path
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _FixedDimModel:
    """Stub model emitting a unit vector of a chosen width."""

    def __init__(self, dim: int):
        self._dim = dim

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * (self._dim - 1)

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed(dim: int) -> None:
    embeddings._MODEL_CACHE["stub"] = _FixedDimModel(dim)  # noqa


def _meta(wg: str, key: str) -> Optional[str]:
    conn = sqlite3.connect(_db_path(wg))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _chunk_count(wg: str) -> int:
    conn = sqlite3.connect(_db_path(wg))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    finally:
        conn.close()


def test_build_index_records_embed_dim(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "some body text\n")
    _seed(8)
    build_index(
        "wg", get_wg_file_cache_dir("wg"),
        model_name="stub", verbose=Verbosity.QUIET,
    )
    assert _meta("wg", "embed_dim") == "8"
    assert _meta("wg", "model") == "stub"


def test_dimension_change_forces_rebuild(isolated_home: Path) -> None:
    cache = get_wg_file_cache_dir("wg")
    write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "some body text\n")

    _seed(8)
    build_index("wg", cache, model_name="stub", verbose=Verbosity.QUIET)
    assert _meta("wg", "embed_dim") == "8"
    assert _chunk_count("wg") > 0

    # Same model id, different dimension -> must rebuild, not mix widths.
    _seed(4)
    build_index("wg", cache, model_name="stub", verbose=Verbosity.QUIET)
    assert _meta("wg", "embed_dim") == "4"
    # Rebuilt cleanly: search runs (no mixed-width matrix corruption) and
    # finds the re-embedded chunk.
    assert search("wg", "anything", verbose=Verbosity.QUIET)
