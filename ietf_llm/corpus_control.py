"""SQLite control plane for the cloud CorpusStore backend.

The control plane is the transactional store that holds, for every corpus: its
published versions and their manifests, the single *current version* pointer
(the linearizable read every request resolves), and the gather *leases* that
serialise writers across hosts. In a cloud deployment this is a managed SQL
database (Postgres family); here it is SQLite, which exercises the same
transactional and versioned semantics and ports to Postgres by swapping the
connection and placeholder dialect. See `docs/cloud-storage.md`.

The program owns the schema: connecting creates the tables if absent, so an
operator provisions an empty database and nothing more. Connections are opened
per operation — SQLite connections are not shareable across threads and the
serve path is multi-threaded — in WAL mode with a busy timeout, so a reader
never blocks on a writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpus_version (
    corpus     TEXT NOT NULL,
    version    TEXT NOT NULL,
    manifest   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (corpus, version)
);
CREATE TABLE IF NOT EXISTS corpus_pointer (
    corpus     TEXT PRIMARY KEY,
    version    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gather_lease (
    corpus      TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the block inside a `BEGIN IMMEDIATE` write transaction: the write
    lock is taken up front (not lazily on first write), so a read-then-write
    test-and-set — the pointer flip and the lease acquire — is atomic against a
    concurrent writer. Commits on success, rolls back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:  # pylint: disable=broad-except
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class ControlPlane:
    """Transactional version pointer + manifest + gather-lease store over a
    SQLite database at `db_path`."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # isolation_level=None -> autocommit; transactions are managed
        # explicitly via _immediate so the write lock is taken up front.
        conn = sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    # --- versions + current pointer ---------------------------------------

    def publish_version(
        self, corpus: str, version: str, manifest: Dict[str, Any]
    ) -> None:
        """Record `version` (with its manifest) and atomically make it the
        current version of `corpus` — both in one transaction, so an
        interruption between them leaves the prior current pointer intact and
        no half-published version is ever pointed at."""
        payload = json.dumps(manifest, sort_keys=True)
        now = _now_iso()
        conn = self._connect()
        try:
            with _immediate(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO corpus_version"
                    " (corpus, version, manifest, created_at) VALUES (?, ?, ?, ?)",
                    (corpus, version, payload, now),
                )
                conn.execute(
                    "INSERT INTO corpus_pointer (corpus, version, updated_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(corpus) DO UPDATE SET version=excluded.version,"
                    " updated_at=excluded.updated_at",
                    (corpus, version, now),
                )
        finally:
            conn.close()

    def resolve_current(self, corpus: str) -> Optional[str]:
        """Current version token for `corpus`, or None if it has none."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT version FROM corpus_pointer WHERE corpus=?", (corpus,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def get_manifest(self, corpus: str, version: str) -> Optional[Dict[str, Any]]:
        """The stored manifest for one (corpus, version), or None if absent.
        The control plane treats the manifest as opaque JSON."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT manifest FROM corpus_version WHERE corpus=? AND version=?",
                (corpus, version),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        loaded: Dict[str, Any] = json.loads(row[0])
        return loaded

    def list_corpora(self) -> List[str]:
        """Corpora that have a current version, sorted."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT corpus FROM corpus_pointer ORDER BY corpus"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    # --- gather lease ------------------------------------------------------

    def acquire_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Try to take the per-corpus gather lease for `owner` for `ttl`
        seconds. Succeeds (returns True) when no *live* lease is held by anyone
        else — i.e. there is no lease, the holder is `owner`, or the holder's
        lease has expired. `now` is overridable for tests."""
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            with _immediate(conn):
                row = conn.execute(
                    "SELECT owner, expires_at FROM gather_lease WHERE corpus=?",
                    (corpus,),
                ).fetchone()
                if row is not None and row[0] != owner and row[1] > clock:
                    return False
                conn.execute(
                    "INSERT INTO gather_lease"
                    " (corpus, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(corpus) DO UPDATE SET owner=excluded.owner,"
                    " acquired_at=excluded.acquired_at,"
                    " expires_at=excluded.expires_at",
                    (corpus, owner, clock, clock + ttl),
                )
                return True
        finally:
            conn.close()

    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Extend `owner`'s lease by `ttl` seconds. Returns False if `owner`
        does not currently hold the lease (so a writer that lost its lease to
        an expiry-and-steal learns it must stop)."""
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            with _immediate(conn):
                cur = conn.execute(
                    "UPDATE gather_lease SET expires_at=? WHERE corpus=? AND owner=?",
                    (clock + ttl, corpus, owner),
                )
                return cur.rowcount > 0
        finally:
            conn.close()

    def release_lease(self, corpus: str, owner: str) -> None:
        """Release `owner`'s lease on `corpus` (no-op if not held by owner)."""
        conn = self._connect()
        try:
            with _immediate(conn):
                conn.execute(
                    "DELETE FROM gather_lease WHERE corpus=? AND owner=?",
                    (corpus, owner),
                )
        finally:
            conn.close()

    def lease_holder(self, corpus: str, now: Optional[float] = None) -> Optional[str]:
        """The owner of the live lease on `corpus`, or None if none is live."""
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT owner, expires_at FROM gather_lease WHERE corpus=?",
                (corpus,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[1] <= clock:
            return None
        return str(row[0])
