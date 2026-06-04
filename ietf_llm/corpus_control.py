"""The control plane for the cloud CorpusStore backend: the transactional store
that holds, per corpus, its published versions and manifests, the single
*current version* pointer (the linearizable read every request resolves), and
the gather *leases* that serialise writers across hosts.

`ControlPlane` is the interface. `SqlControlPlane` implements it against any
DB-API 2.0 SQL database, writing **portable** SQL — the two operations that must
be atomic are expressed without any single-engine trick:

  - the **lease** test-and-set is a *single* conditional upsert
    (`INSERT … ON CONFLICT DO UPDATE … WHERE … RETURNING`), atomic at the
    statement level on every engine;
  - **publish** records the version and flips the pointer in one DB-API
    transaction (`commit()` / `rollback()`), not a `BEGIN IMMEDIATE`.

So the same implementation runs on SQLite, Postgres, and libSQL; a subclass only
supplies a connection and the parameter placeholder. `SqliteControlPlane` is the
bundled single-host / development backend (SQLite is a local file — it
coordinates processes on one host, not writers across hosts); `PostgresControlPlane`
(see `[postgres]` extra) is the multi-host production backend. The program owns
the schema (created on connect). See `docs/storage.md`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Schema as individual statements (not a SQLite `executescript`), each created
# if absent so the program owns the schema on any engine. `DOUBLE PRECISION`
# has REAL affinity on SQLite and is native on Postgres.
_SCHEMA_STMTS = (
    "CREATE TABLE IF NOT EXISTS corpus_version ("
    " corpus TEXT NOT NULL, version TEXT NOT NULL,"
    " manifest TEXT NOT NULL, created_at TEXT NOT NULL,"
    " PRIMARY KEY (corpus, version))",
    "CREATE TABLE IF NOT EXISTS corpus_pointer ("
    " corpus TEXT PRIMARY KEY, version TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS gather_lease ("
    " corpus TEXT PRIMARY KEY, owner TEXT NOT NULL,"
    " acquired_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ControlPlane(ABC):
    """Transactional version-pointer + manifest + gather-lease store. The cloud
    CorpusStore composes one of these with a `BlobStore`."""

    @abstractmethod
    def publish_version(
        self, corpus: str, version: str, manifest: Dict[str, Any]
    ) -> None:
        """Record `version` (+ manifest) and atomically make it current."""

    @abstractmethod
    def resolve_current(self, corpus: str) -> Optional[str]:
        """Current version token for `corpus`, or None."""

    @abstractmethod
    def get_manifest(self, corpus: str, version: str) -> Optional[Dict[str, Any]]:
        """The stored manifest for one (corpus, version), or None."""

    @abstractmethod
    def list_corpora(self) -> List[str]:
        """Corpora that have a current version, sorted."""

    @abstractmethod
    def acquire_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Take the per-corpus gather lease for `owner` for `ttl` seconds;
        True if granted (no live lease held by anyone else)."""

    @abstractmethod
    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Extend `owner`'s lease; False if `owner` no longer holds it."""

    @abstractmethod
    def release_lease(self, corpus: str, owner: str) -> None:
        """Release `owner`'s lease (no-op if not held by owner)."""

    @abstractmethod
    def lease_holder(self, corpus: str, now: Optional[float] = None) -> Optional[str]:
        """Owner of the live lease on `corpus`, or None."""


class SqlControlPlane(ControlPlane):
    """Portable DB-API 2.0 implementation. Subclasses supply `_connect()` (a
    fresh connection per call — connections are opened per operation, since the
    serve path is multi-threaded) and `_param` (the placeholder marker). The SQL
    below is written once with `?` placeholders and translated if needed."""

    #: Parameter placeholder for this engine: "?" (sqlite, libsql) or "%s" (psycopg).
    _param: str = "?"

    def _connect(self) -> Any:
        raise NotImplementedError

    def _sql(self, query: str) -> str:
        return query if self._param == "?" else query.replace("?", self._param)

    def _ensure_schema(self, conn: Any) -> None:
        for stmt in _SCHEMA_STMTS:
            conn.execute(stmt)
        conn.commit()

    # --- versions + current pointer ---------------------------------------

    def publish_version(
        self, corpus: str, version: str, manifest: Dict[str, Any]
    ) -> None:
        payload = json.dumps(manifest, sort_keys=True)
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                self._sql(
                    "INSERT INTO corpus_version"
                    " (corpus, version, manifest, created_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (corpus, version) DO UPDATE SET"
                    " manifest=excluded.manifest, created_at=excluded.created_at"
                ),
                (corpus, version, payload, now),
            )
            conn.execute(
                self._sql(
                    "INSERT INTO corpus_pointer (corpus, version, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT (corpus) DO UPDATE SET"
                    " version=excluded.version, updated_at=excluded.updated_at"
                ),
                (corpus, version, now),
            )
            conn.commit()
        except BaseException:  # pylint: disable=broad-except
            conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_current(self, corpus: str) -> Optional[str]:
        conn = self._connect()
        try:
            cur = conn.execute(
                self._sql("SELECT version FROM corpus_pointer WHERE corpus=?"),
                (corpus,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else None

    def get_manifest(self, corpus: str, version: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.execute(
                self._sql(
                    "SELECT manifest FROM corpus_version"
                    " WHERE corpus=? AND version=?"
                ),
                (corpus, version),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        loaded: Dict[str, Any] = json.loads(row[0])
        return loaded

    def list_corpora(self) -> List[str]:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT corpus FROM corpus_pointer ORDER BY corpus")
            rows = cur.fetchall()
        finally:
            conn.close()
        return [str(r[0]) for r in rows]

    # --- gather lease ------------------------------------------------------

    def acquire_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        # Single-statement atomic test-and-set: insert the lease, or on conflict
        # take it over only if the held lease has expired or is already ours.
        # RETURNING yields a row iff we now hold it.
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            cur = conn.execute(
                self._sql(
                    "INSERT INTO gather_lease (corpus, owner, acquired_at, expires_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (corpus) DO UPDATE SET owner=excluded.owner,"
                    " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at"
                    " WHERE gather_lease.expires_at <= ? OR gather_lease.owner = ?"
                    " RETURNING owner"
                ),
                (corpus, owner, clock, clock + ttl, clock, owner),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None
        except BaseException:  # pylint: disable=broad-except
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            cur = conn.execute(
                self._sql(
                    "UPDATE gather_lease SET expires_at=? WHERE corpus=? AND owner=?"
                ),
                (clock + ttl, corpus, owner),
            )
            conn.commit()
            return bool(cur.rowcount and cur.rowcount > 0)
        finally:
            conn.close()

    def release_lease(self, corpus: str, owner: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                self._sql("DELETE FROM gather_lease WHERE corpus=? AND owner=?"),
                (corpus, owner),
            )
            conn.commit()
        finally:
            conn.close()

    def lease_holder(self, corpus: str, now: Optional[float] = None) -> Optional[str]:
        clock = time.time() if now is None else now
        conn = self._connect()
        try:
            cur = conn.execute(
                self._sql("SELECT owner, expires_at FROM gather_lease WHERE corpus=?"),
                (corpus,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if row is None or row[1] <= clock:
            return None
        return str(row[0])


class SqliteControlPlane(SqlControlPlane):
    """Bundled single-host / development backend: a local SQLite file. SQLite
    coordinates processes on one host (WAL + busy timeout) but not writers
    across hosts — a multi-host fleet wants `PostgresControlPlane`."""

    _param = "?"

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        return conn
