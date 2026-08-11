"""Upgrading an older index in place, on read (#230).

A schema bump would otherwise take `search_corpus` away from anyone who does
not gather: the read path cannot migrate, so an index one release old is
refused until its owner happens to run a gather — which a read-only MCP user
may never do. For two `ALTER TABLE`s that is a poor trade.

What matters is that the exception stays narrow: cheap steps only, never on
an index that is not ours to write, and always falling back to the old
guidance rather than failing.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.embeddings.storage import (
    _AUTO_UPGRADE_FROM,
    _SCHEMA_VERSION,
    _db_path,
    try_upgrade_schema,
)
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _built(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-01-01-t.md",
        "# T\n\n## Messages\n\n### [1] 2026-01-01 09:00 — A\n\nA response may be stored.\n",
    )
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index("wg", get_wg_file_cache_dir("wg"), model_name="stub",
                verbose=Verbosity.QUIET)


def _age_to(version: int, drop_columns: bool = True) -> None:
    """Make the on-disk index look like an older schema."""
    conn = sqlite3.connect(_db_path("wg"))
    try:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
        if drop_columns:
            conn.execute("DROP INDEX IF EXISTS chunks_cluster")
            for col in ("cluster_id", "section"):
                try:
                    conn.execute(f"ALTER TABLE chunks DROP COLUMN {col}")
                except sqlite3.Error:
                    pass
        conn.commit()
    finally:
        conn.close()


def _schema(wg: str = "wg") -> int:
    conn = sqlite3.connect(_db_path(wg))
    try:
        return int(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
    finally:
        conn.close()


def test_search_upgrades_an_older_index_instead_of_refusing(
    isolated_home: Path,
) -> None:
    """The whole point: a read-only user keeps working across a bump."""
    _built(isolated_home)
    _age_to(_AUTO_UPGRADE_FROM)
    assert _schema() == _AUTO_UPGRADE_FROM

    hits = search("wg", "stored response", k=1)
    assert hits, "search refused an index it could have upgraded"
    assert _schema() == _SCHEMA_VERSION


def test_the_upgrade_adds_the_missing_columns(isolated_home: Path) -> None:
    _built(isolated_home)
    _age_to(_AUTO_UPGRADE_FROM)
    assert try_upgrade_schema("wg", _AUTO_UPGRADE_FROM)
    conn = sqlite3.connect(_db_path("wg"))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    finally:
        conn.close()
    assert {"section", "cluster_id"} <= cols


def test_an_expensive_step_is_left_to_a_gather(isolated_home: Path) -> None:
    """Below the floor the work is real — a table rebuild at v7→v8, a full
    text hash at v8→v9 — and belongs where the user expects to wait."""
    _built(isolated_home)
    _age_to(_AUTO_UPGRADE_FROM - 1)
    assert not try_upgrade_schema("wg", _AUTO_UPGRADE_FROM - 1)
    assert _schema() == _AUTO_UPGRADE_FROM - 1


def test_an_immutable_index_is_never_written(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published replica upgrades by being republished, not by a reader
    mutating a materialised copy."""
    _built(isolated_home)
    _age_to(_AUTO_UPGRADE_FROM)
    monkeypatch.setenv("IETF_LLM_INDEX_IMMUTABLE", "1")
    assert not try_upgrade_schema("wg", _AUTO_UPGRADE_FROM)
    assert _schema() == _AUTO_UPGRADE_FROM


def test_a_current_index_is_not_touched(isolated_home: Path) -> None:
    _built(isolated_home)
    before = os.path.getmtime(_db_path("wg"))
    assert search("wg", "stored response", k=1)
    assert os.path.getmtime(_db_path("wg")) == before
