"""search() must read the index read-only: no makedirs, no WAL switch,
no ALTER-TABLE migration. A shared server may run against an immutable
index, so the query path must never write. An index whose on-disk schema
predates the current version is reported, not silently migrated.
"""

from __future__ import annotations

import os
import sqlite3

from ietf_llm.embeddings.search import search
from ietf_llm.embeddings.storage import _db_path
from ietf_llm.utils import Verbosity


def _write_old_schema_db(wg: str) -> str:
    # A pre-faceted-columns index: chunks without start_line/labels/state/
    # etc. and a meta table with a model but no schema_version row (so it
    # reads as v1).
    path = _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file TEXT, "
        "chunk_idx INTEGER, title TEXT, text TEXT, embedding BLOB)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO meta VALUES ('model', 'sentence-transformers/x')"
    )
    conn.commit()
    conn.close()
    return path


def test_search_old_schema_is_graceful_and_does_not_migrate(isolated_home):
    wg = "oldwg"
    path = _write_old_schema_db(wg)

    # Old schema -> empty result with guidance, never a "no such column".
    assert search(wg, "anything", verbose=Verbosity.QUIET) == []

    # And the read must not have migrated the DB (no new columns added).
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    has_sv = conn.execute(
        "SELECT 1 FROM meta WHERE key='schema_version'"
    ).fetchone()
    conn.close()
    assert "labels" not in cols
    assert "start_line" not in cols
    assert has_sv is None


def test_search_missing_index_returns_empty(isolated_home):
    # No DB at all: still graceful, and no cache dir is materialised by
    # the read path.
    assert search("nope", "q", verbose=Verbosity.QUIET) == []
    assert not os.path.exists(_db_path("nope"))
