"""The control plane for the cloud CorpusStore backend: the transactional store
that holds, per corpus, its published versions and manifests, the single
*current version* pointer (the linearizable read every request resolves), and
the gather *leases* that serialise writers across hosts.

`ControlPlane` is the interface. `SqlControlPlane` implements it over a
**pluggable SQL executor** — the `SqlExecutor` seam — with two primitives:

  - `query(sql, params)` runs **one** statement and returns its rows;
  - `batch(statements)` runs several statements **atomically, in one round
    trip**.

Everything is SQLite dialect (`?` placeholders), and the two operations that
must be atomic are shaped so a *stateless HTTP* database works exactly like a
local file: the lease test-and-set is a single conditional upsert with
`RETURNING` (one `query`), and publish is a two-statement `batch` (one round
trip) — there are no interactive multi-round-trip transactions. So the same
`SqlControlPlane` runs over a local SQLite file (`SqliteExecutor`) or a cloud
SQLite-compatible database reached over its HTTP API (e.g. the Cloudflare D1
adapter in `corpus_control_d1`). The program owns the schema. See
`docs/storage.md`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Schema as individual `CREATE TABLE IF NOT EXISTS` statements (portable across
#: SQLite, D1, libSQL). `DOUBLE PRECISION` has REAL affinity on SQLite and is a
#: real type on the others.
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
    "CREATE TABLE IF NOT EXISTS corpus_status ("
    " corpus TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)",
    # Fleet-wide gather concurrency: one row per *actively running* gather
    # (keyed by the gatherer's owner id), expiring on a TTL like a lease. The
    # live row count is the global slot occupancy; the cap is enforced at
    # acquire time. A queued gather holds its per-corpus lease but no slot.
    "CREATE TABLE IF NOT EXISTS gather_active ("
    " owner TEXT PRIMARY KEY, corpus TEXT NOT NULL,"
    " expires_at DOUBLE PRECISION NOT NULL)",
)

#: A statement plus its positional parameters.
Statement = Tuple[str, Sequence[Any]]
Row = Tuple[Any, ...]

#: Per-process guard: SQLite db paths whose schema has been ensured this run, so
#: the `CREATE TABLE IF NOT EXISTS` batch is not re-issued on every per-request
#: executor construction (the D1 executor has its own equivalent guard).
_sqlite_schema_ensured: set[str] = set()


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

    @abstractmethod
    def acquire_gather_slot(
        self,
        owner: str,
        corpus: str,
        ttl: float,
        max_inflight: int,
        now: Optional[float] = None,
    ) -> bool:
        """Claim one of the `max_inflight` fleet-wide gather slots for `owner`
        for `ttl` seconds; True if granted (fewer than `max_inflight` other
        live slots). Idempotent for an owner that already holds one."""

    @abstractmethod
    def renew_gather_slot(
        self, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        """Extend `owner`'s gather slot; False if `owner` no longer holds one."""

    @abstractmethod
    def release_gather_slot(self, owner: str) -> None:
        """Release `owner`'s gather slot (no-op if not held)."""

    @abstractmethod
    def set_gather_status(self, corpus: str, payload: str) -> None:
        """Store the latest gather status (opaque JSON) for `corpus`, so it is
        visible to any replica, not just the one running the gather."""

    @abstractmethod
    def get_gather_status(self, corpus: str) -> Optional[str]:
        """The stored gather status for `corpus`, or None if none recorded."""

    @abstractmethod
    def list_gather_statuses(self) -> List[Tuple[str, str]]:
        """Every stored gather status as `(corpus, payload)` pairs, so a
        no-corpus `gather_status` listing sees gathers on other replicas, not
        just the locally-cached corpora this host knows about."""


class SqlExecutor(ABC):
    """Runs SQLite-dialect SQL against a backend. Two primitives, each a single
    round trip, so a stateless HTTP database (D1, libSQL) behaves like a local
    file. Implementations open/close per call as needed — the serve path is
    multi-threaded."""

    @abstractmethod
    def ensure_schema(self, statements: Sequence[str]) -> None:
        """Apply the schema DDL (idempotent `CREATE TABLE IF NOT EXISTS`)."""

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        """Run one statement; return its rows (empty for a write without
        `RETURNING`)."""

    @abstractmethod
    def batch(self, statements: Sequence[Statement]) -> None:
        """Run several statements atomically (all-or-nothing) in one unit."""


class SqlControlPlane(ControlPlane):
    """Control plane implemented over a `SqlExecutor`. Backend-agnostic: the
    same logic runs over SQLite or any SQLite-compatible cloud database, since
    every operation is one `query` or one atomic `batch`."""

    def __init__(self, executor: SqlExecutor) -> None:
        self._sql = executor
        self._sql.ensure_schema(_SCHEMA_STMTS)

    # --- versions + current pointer ---------------------------------------

    def publish_version(
        self, corpus: str, version: str, manifest: Dict[str, Any]
    ) -> None:
        # Record the version and flip the pointer in one atomic batch (one round
        # trip): an interruption leaves the prior version current and the new
        # version unreferenced, never a torn read.
        payload = json.dumps(manifest, sort_keys=True)
        now = _now_iso()
        self._sql.batch(
            [
                (
                    "INSERT INTO corpus_version"
                    " (corpus, version, manifest, created_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (corpus, version) DO UPDATE SET"
                    " manifest=excluded.manifest, created_at=excluded.created_at",
                    (corpus, version, payload, now),
                ),
                (
                    "INSERT INTO corpus_pointer (corpus, version, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT (corpus) DO UPDATE SET"
                    " version=excluded.version, updated_at=excluded.updated_at",
                    (corpus, version, now),
                ),
            ]
        )

    def resolve_current(self, corpus: str) -> Optional[str]:
        rows = self._sql.query(
            "SELECT version FROM corpus_pointer WHERE corpus=?", (corpus,)
        )
        return str(rows[0][0]) if rows else None

    def get_manifest(self, corpus: str, version: str) -> Optional[Dict[str, Any]]:
        rows = self._sql.query(
            "SELECT manifest FROM corpus_version WHERE corpus=? AND version=?",
            (corpus, version),
        )
        if not rows:
            return None
        loaded: Dict[str, Any] = json.loads(rows[0][0])
        return loaded

    def list_corpora(self) -> List[str]:
        rows = self._sql.query("SELECT corpus FROM corpus_pointer ORDER BY corpus")
        return [str(r[0]) for r in rows]

    # --- gather lease ------------------------------------------------------

    def acquire_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        # Single-statement atomic test-and-set: insert the lease, or on conflict
        # take it over only if the held lease has expired or is already ours.
        # RETURNING yields a row iff we now hold it.
        clock = time.time() if now is None else now
        rows = self._sql.query(
            "INSERT INTO gather_lease (corpus, owner, acquired_at, expires_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (corpus) DO UPDATE SET owner=excluded.owner,"
            " acquired_at=excluded.acquired_at, expires_at=excluded.expires_at"
            " WHERE gather_lease.expires_at <= ? OR gather_lease.owner = ?"
            " RETURNING owner",
            (corpus, owner, clock, clock + ttl, clock, owner),
        )
        return bool(rows)

    def renew_lease(
        self, corpus: str, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        rows = self._sql.query(
            "UPDATE gather_lease SET expires_at=?"
            " WHERE corpus=? AND owner=? RETURNING owner",
            (clock + ttl, corpus, owner),
        )
        return bool(rows)

    def release_lease(self, corpus: str, owner: str) -> None:
        self._sql.query(
            "DELETE FROM gather_lease WHERE corpus=? AND owner=?", (corpus, owner)
        )

    def lease_holder(self, corpus: str, now: Optional[float] = None) -> Optional[str]:
        clock = time.time() if now is None else now
        rows = self._sql.query(
            "SELECT owner, expires_at FROM gather_lease WHERE corpus=?", (corpus,)
        )
        if not rows or rows[0][1] <= clock:
            return None
        return str(rows[0][0])

    # --- fleet-wide gather slots (global concurrency cap) ------------------

    def acquire_gather_slot(
        self,
        owner: str,
        corpus: str,
        ttl: float,
        max_inflight: int,
        now: Optional[float] = None,
    ) -> bool:
        # Single-statement atomic admission: insert our slot only if fewer than
        # `max_inflight` *other* live slots exist (excluding our own row so a
        # re-acquire is idempotent and never self-blocks). ON CONFLICT refreshes
        # our own row. RETURNING yields a row iff we now hold a slot. Statement-
        # level atomicity makes the count-then-insert safe under concurrent
        # acquirers across hosts (the DB serialises writers).
        clock = time.time() if now is None else now
        rows = self._sql.query(
            "INSERT INTO gather_active (owner, corpus, expires_at)"
            " SELECT ?, ?, ?"
            " WHERE (SELECT COUNT(*) FROM gather_active"
            "        WHERE expires_at > ? AND owner <> ?) < ?"
            " ON CONFLICT (owner) DO UPDATE SET"
            " corpus=excluded.corpus, expires_at=excluded.expires_at"
            " RETURNING owner",
            (owner, corpus, clock + ttl, clock, owner, max_inflight),
        )
        return bool(rows)

    def renew_gather_slot(
        self, owner: str, ttl: float, now: Optional[float] = None
    ) -> bool:
        clock = time.time() if now is None else now
        rows = self._sql.query(
            "UPDATE gather_active SET expires_at=? WHERE owner=? RETURNING owner",
            (clock + ttl, owner),
        )
        return bool(rows)

    def release_gather_slot(self, owner: str) -> None:
        self._sql.query("DELETE FROM gather_active WHERE owner=?", (owner,))

    def set_gather_status(self, corpus: str, payload: str) -> None:
        self._sql.query(
            "INSERT INTO corpus_status (corpus, status, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT (corpus) DO UPDATE SET"
            " status=excluded.status, updated_at=excluded.updated_at",
            (corpus, payload, _now_iso()),
        )

    def get_gather_status(self, corpus: str) -> Optional[str]:
        rows = self._sql.query(
            "SELECT status FROM corpus_status WHERE corpus=?", (corpus,)
        )
        return str(rows[0][0]) if rows else None

    def list_gather_statuses(self) -> List[Tuple[str, str]]:
        rows = self._sql.query("SELECT corpus, status FROM corpus_status")
        return [(str(row[0]), str(row[1])) for row in rows]


class SqliteExecutor(SqlExecutor):
    """Local-file `SqlExecutor`: a SQLite database. Coordinates processes on one
    host (WAL + busy timeout) but not writers across hosts — for that, a
    cloud SQL executor (e.g. D1). Opens a connection per call (thread-safe)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def ensure_schema(self, statements: Sequence[str]) -> None:
        if self._db_path in _sqlite_schema_ensured:
            return
        self.batch([(stmt, ()) for stmt in statements])
        _sqlite_schema_ensured.add(self._db_path)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        conn = self._connect()
        try:
            cur = conn.execute(sql, tuple(params))
            rows = [tuple(r) for r in cur.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def batch(self, statements: Sequence[Statement]) -> None:
        conn = self._connect()
        try:
            for sql, params in statements:
                conn.execute(sql, tuple(params))
            conn.commit()
        except BaseException:  # pylint: disable=broad-except
            conn.rollback()
            raise
        finally:
            conn.close()


class SqliteControlPlane(SqlControlPlane):
    """Convenience: a `SqlControlPlane` over a local SQLite file at `db_path`."""

    def __init__(self, db_path: str) -> None:
        super().__init__(SqliteExecutor(db_path))
