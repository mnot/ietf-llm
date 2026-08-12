# pylint: disable=too-many-lines
"""SQLite layer for the per-WG embedding index.

One DB per WG at <index-dir>/<wg>/embeddings.db (the index dir defaults to
the cache root; see paths.get_index_dir), with two tables:

  chunks(id, file, chunk_idx, title, text, embedding)
      One row per indexed chunk. The embedding column holds a packed
      float32 vector (already L2-normalised so search is a dot product).

  meta(key, value)
      Per-index metadata: the model id used to produce the vectors,
      and one `hash:<filename>` row (the file's SHA-256) per indexed
      file for incremental re-embedding.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from .. import serve_metrics
from ..atomicio import file_lock
from ..paths import get_index_dir

#: Bumped when the chunks-table schema changes. _open_db migrates older
#: databases forward via ALTER TABLE so users don't have to re-embed,
#: but newly-indexed chunks will get the richer metadata; rows from the
#: pre-migration era will have NULL in the new columns until the user
#: runs `--rebuild-embeddings`.
_SCHEMA_VERSION = 11

#: Trailing "(part k/n)" hint the chunker appends to the title of a split
#: message's fragments (for search-hit legibility). Stripped when a read
#: path reconstitutes the whole message, so callers never see it.
_PART_HINT_RE = re.compile(r"\s*\(part \d+/\d+\)$")


def _clean_title(title: str) -> str:
    """Drop the chunker's `(part k/n)` fragment hint from a stored title."""
    return _PART_HINT_RE.sub("", title)


def _db_path(wg: str) -> str:
    # The index root defaults to the cache root but can be pointed at fast
    # / RAM-backed storage (tmpfs) via IETF_LLM_INDEX_DIR; see get_index_dir.
    # This is the *write* path (build_index); reads go through _db_path_ro.
    return os.path.join(get_index_dir(), wg, "embeddings.db")


def _db_building_path(wg: str) -> str:
    """The scratch path a build writes to before atomically swapping it over
    the live `embeddings.db`. Same directory as the live DB (so `os.replace`
    is atomic on one filesystem), suffixed so it never collides with a read."""
    return _db_path(wg) + ".building"


def _remove_db_files(path: str) -> None:
    """Delete a SQLite DB and any WAL/SHM sidecars, ignoring absence."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _clonefile(src: str, dst: str) -> None:
    """Copy-on-write clone via macOS `clonefile(2)`. Raises `OSError` if the
    clone can't be made (wrong filesystem, cross-volume, dst exists, ...)."""
    libc = ctypes.CDLL(None, use_errno=True)
    clone = libc.clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(src), os.fsencode(dst), 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def _copy_file_range(src: str, dst: str) -> None:
    """Copy via Linux `copy_file_range(2)` — a reflink on a copy-on-write
    filesystem (btrfs, XFS-with-reflink), an efficient in-kernel copy elsewhere.
    Raises `OSError`/`AttributeError` where unavailable.

    `copy_file_range` is Linux-only, so it is reached via `getattr` (typeshed
    hides it off Linux); the `is None` guard makes the call safe."""
    copy_range = getattr(os, "copy_file_range", None)
    if copy_range is None:
        raise AttributeError("os.copy_file_range unavailable")
    # pylint: disable=not-callable  # guarded above; getattr() infers as Any/None
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        remaining = os.fstat(fsrc.fileno()).st_size
        while remaining > 0:
            copied = copy_range(fsrc.fileno(), fdst.fileno(), remaining)
            if copied == 0:
                break  # short of EOF should not happen for a regular file
            remaining -= copied


def _clone_or_copy(src: str, dst: str) -> None:
    """Copy `src` to `dst`, preferring a copy-on-write clone where the platform
    and filesystem support it (near-free), falling back to a full byte copy.

    The scratch build DB is otherwise a full copy of the live index on every
    gather, incremental or not; a reflink makes that near-free on APFS (macOS)
    and btrfs / XFS-with-reflink (Linux). No metadata need survive the copy —
    the build keys incremental skips on content hashes, not mtime — and the
    clone is an independent object, so mutating the scratch never touches live."""
    try:
        if sys.platform == "darwin":
            _clonefile(src, dst)
        else:
            _copy_file_range(src, dst)
        return
    except (OSError, AttributeError):
        # CoW clone unsupported here (wrong fs, cross-device, ENOSYS, ...); the
        # partial dst, if any, is cleared before the portable full-copy path.
        _remove_db_files(dst)
    shutil.copy2(src, dst)


def seed_build_db(wg: str) -> str:
    """Prepare the scratch build DB and return its path.

    A build mutates this scratch copy, not the live index, so a concurrent
    reader never sees a half-populated DB — only the final atomic swap
    (`promote_build_db`) makes new content visible. Seeding from the current
    live index (a copy-on-write clone where the filesystem supports it, else a
    full copy) keeps the build incremental: unchanged files keep their
    embeddings and are skipped. Any leftover scratch from a
    killed prior build is discarded here and rebuilt from the live index, so a
    crash costs only the interrupted run's work, never the published index."""
    live = _db_path(wg)
    building = _db_building_path(wg)
    _remove_db_files(building)
    if os.path.exists(live):
        os.makedirs(os.path.dirname(building), exist_ok=True)
        _clone_or_copy(live, building)
    return building


def promote_build_db(wg: str) -> None:
    """Atomically swap the finished scratch build over the live index.

    `_build_index_locked` leaves the scratch DB in DELETE journal mode (a
    standalone file, no `-wal`/`-shm`), so the swap is a single `os.replace`
    and a reader sees the whole old index or the whole new one, never an
    intermediate. Stale sidecars from the previous (possibly WAL-era) inode
    are cleared so a reader never pairs the new DB with an old WAL."""
    live = _db_path(wg)
    building = _db_building_path(wg)
    os.replace(building, live)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(live + suffix)
        except OSError:
            pass


def discard_build_db(wg: str) -> None:
    """Drop the scratch build DB (e.g. after a failed build). The live index
    is untouched, so the next gather reseeds from it."""
    _remove_db_files(_db_building_path(wg))


def _db_path_ro(wg: str) -> str:
    """The index DB path for a *read*, resolved through the corpus store so a
    cloud reader replica serves the current version's `embeddings.db`
    (materialised onto local scratch) instead of an empty local index dir. The
    local backend returns `<index_root>/<wg>`, so local reads are unchanged."""
    from ..store.corpus import (  # pylint: disable=import-outside-toplevel
        get_corpus_store,
    )

    index_dir = serve_metrics.timed_store(
        "local_index_dir", lambda: get_corpus_store().local_index_dir(wg)
    )
    if index_dir is None:
        index_dir = os.path.join(get_index_dir(), wg)
    return os.path.join(index_dir, "embeddings.db")


# Wait up to this long for a lock instead of failing immediately, so a
# reader (e.g. an MCP query) and a writer (a gather rebuilding the
# index) can overlap on the same WG without "database is locked".
_BUSY_TIMEOUT_S = 30.0


def _connect(path: str, *, write: bool = False) -> sqlite3.Connection:
    """Open the index DB with a busy timeout. Writers also switch the DB
    to WAL (persistent), which lets readers proceed during a write."""
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S)
    if write:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _index_immutable() -> bool:
    """Whether to open index reads with SQLite's ``immutable=1``.

    Off by default. On (``IETF_LLM_INDEX_IMMUTABLE=1``) for a served
    replica that is published-and-swapped and never written in place --
    the only way to read a WAL-mode DB from a read-only mount, where the
    ``-shm`` sidecar a plain open would need cannot be created.
    """
    return os.environ.get("IETF_LLM_INDEX_IMMUTABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _connect_ro(wg: str) -> sqlite3.Connection:
    """Read-only connection to a WG index (busy timeout, no schema work).

    Default: a plain connection. It can create the WAL's ``-shm`` sidecar
    on a writable index dir, which a checkpointed WAL database needs in
    order to be read -- correct for the local CLI and for a writable
    (tmpfs) served index. For an immutable replica on a read-only mount,
    set ``IETF_LLM_INDEX_IMMUTABLE=1`` (see ``_index_immutable``): SQLite
    then reads the file directly, skipping WAL/-shm and locking. Only safe
    when nothing rewrites the file in place.
    """
    path = _db_path_ro(wg)
    if _index_immutable():
        uri = f"{Path(os.path.abspath(path)).as_uri()}?immutable=1"
        return sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_S)
    return _connect(path)


def _open_db(wg: str, path: Optional[str] = None) -> sqlite3.Connection:
    path = path or _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = _connect(path, write=True)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id         INTEGER PRIMARY KEY,
            file       TEXT NOT NULL,
            chunk_idx  INTEGER NOT NULL,
            sub_idx    INTEGER NOT NULL DEFAULT 0,  -- fragment within a long section
            title      TEXT NOT NULL,
            text       TEXT NOT NULL,
            embedding  BLOB NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            chunk_date TEXT,              -- ISO 8601 UTC, NULL for undated chunks
            labels     TEXT,              -- comma-separated, lowercased; for issue chunks
            state      TEXT,              -- 'open'/'closed' for issue chunks; NULL elsewhere
            url        TEXT,              -- citation URL: GitHub issue / Archived-At / draft / charter; NULL elsewhere
            duplicate_of INTEGER,          -- issue chunks only: this issue marked dup of #N
            closing_rationale TEXT,        -- issue chunks only: last comment body when closed
            chunk_hash TEXT,               -- SHA-256 of the embedded text; per-chunk incremental reuse
            section    TEXT,               -- document section label ('7.2', 'A.1'); RFC corpus only
            cluster_id INTEGER,             -- IVF partition, when the index has one
            UNIQUE (file, chunk_idx, sub_idx)
        )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS centroids (
            id     INTEGER PRIMARY KEY,
            vector BLOB NOT NULL
        )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)
    _migrate(conn)
    # After the migration, not before: on an existing database `cluster_id` is
    # added by `_migrate`, so indexing it any earlier fails on the column not
    # existing yet — which locked every pre-v11 index out of being opened.
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_cluster ON chunks(cluster_id)")
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
    # v6 → v7: per-issue cluster signals — `duplicate_of` (the #N this
    # issue is a dup of, when called out in any comment) and
    # `closing_rationale` (last comment body when state=closed). Both
    # are file-level metadata applied to every chunk from the issue,
    # so an LLM scanning search hits sees "this is a dup" / "closed
    # because X" inline without opening the file.
    if "duplicate_of" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN duplicate_of INTEGER")
    if "closing_rationale" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN closing_rationale TEXT")
    # v7 → v8: per-section sub-fragment ordinal. Adding the column would be a
    # plain ALTER, but the UNIQUE constraint must also widen from
    # (file, chunk_idx) to (file, chunk_idx, sub_idx) so one long message can
    # own several embedding rows. SQLite can't ALTER a constraint, so we
    # recreate the table, carrying existing rows forward as sub_idx 0. They
    # stay searchable as-is; a CHUNKER_VERSION mismatch re-embeds each WG on
    # its next gather, which is when the finer-grained fragments appear.
    if "sub_idx" not in have:
        conn.execute("""
            CREATE TABLE chunks_v8 (
                id         INTEGER PRIMARY KEY,
                file       TEXT NOT NULL,
                chunk_idx  INTEGER NOT NULL,
                sub_idx    INTEGER NOT NULL DEFAULT 0,
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
                UNIQUE (file, chunk_idx, sub_idx)
            )
            """)
        conn.execute("""
            INSERT INTO chunks_v8
                (id, file, chunk_idx, sub_idx, title, text, embedding,
                 start_line, end_line, chunk_date, labels, state, url,
                 duplicate_of, closing_rationale)
            SELECT id, file, chunk_idx, 0, title, text, embedding,
                   start_line, end_line, chunk_date, labels, state, url,
                   duplicate_of, closing_rationale
            FROM chunks
            """)
        conn.execute("DROP TABLE chunks")
        conn.execute("ALTER TABLE chunks_v8 RENAME TO chunks")

    # v8 → v9: per-chunk content hash for per-chunk incremental re-embedding
    # (issue #183) — a changed file re-embeds only its changed chunks (e.g. a
    # thread that gains one message embeds that message, not all of them). Added
    # as a plain column (existing vectors preserved, no re-embed) and backfilled
    # from stored text for the chunks that embedded their full text, so the win
    # applies immediately without a rebuild.
    if "chunk_hash" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN chunk_hash TEXT")
        _backfill_chunk_hash(conn)

    # v9 → v10: the document section label a chunk belongs to ("7.2", "A.1").
    # Only the imported RFC corpus populates it — a mailing list has no such
    # structure — and it is what `get_rfc_section` looks up. NULL for every
    # chunk a gather writes, now and before, so nothing needs backfilling.
    if "section" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN section TEXT")

    # v10 → v11: the IVF partition an imported index arrives already carrying.
    # NULL for a gathered corpus, which has no partition and is scanned whole —
    # so this costs an existing index one ALTER and nothing else.
    if "cluster_id" not in have:
        conn.execute("ALTER TABLE chunks ADD COLUMN cluster_id INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS centroids (
            id     INTEGER PRIMARY KEY,
            vector BLOB NOT NULL
        )
        """)

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    conn.commit()


def chunk_hash(text: str) -> str:
    """The per-chunk identity/change key: SHA-256 of the exact text embedded
    (`embed_text` when a long section was windowed, else the chunk's full
    `text`). Two chunks with the same embedded text yield the same vector under a
    fixed model, so a matching hash means the stored vector can be reused."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backfill_chunk_hash(conn: sqlite3.Connection) -> None:
    """Populate `chunk_hash` for existing rows without re-embedding (v8 → v9).

    A chunk embedded its full `text` unless its section was windowed into
    fragments — i.e. unless its `(file, chunk_idx)` group has any `sub_idx > 0`
    row. For those non-windowed chunks the embedded text is exactly the stored
    `text`, so `chunk_hash` is recomputable here. Windowed chunks (rare long
    messages) are left NULL and re-embed on their next change, which stamps the
    hash going forward."""
    split = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT DISTINCT file, chunk_idx FROM chunks WHERE sub_idx > 0"
        )
    }
    updates = [
        (chunk_hash(text), cid)
        for cid, file, cidx, text in conn.execute(
            "SELECT id, file, chunk_idx, text FROM chunks WHERE chunk_hash IS NULL"
        )
        if (file, cidx) not in split
    ]
    conn.executemany("UPDATE chunks SET chunk_hash=? WHERE id=?", updates)


#: How the `chunks.embedding` blob is encoded. `float32` is what a gather
#: writes and what every existing index holds; `int8` exists so an index
#: *imported* from vectors that are already quantised can keep them that way
#: (issue #230: rfc.fyi publishes the RFC series at 384 int8 dimensions, and
#: dequantising on import would inflate 167 MiB to 670 MiB to no benefit —
#: the precision is already gone).
ENCODING_FLOAT32 = "float32"
ENCODING_INT8 = "int8"

#: Meta keys recording the encoding. Absent means `float32`, which is what
#: every index written before this existed holds — so no migration, and no
#: schema bump: `meta` is key-value, and an older reader simply never asks.
_META_ENCODING = "vector_encoding"
_META_SCALE = "vector_scale"

#: Provenance for a corpus imported from an upstream artifact rather than
#: gathered: which build it came from, and the commit that produced it.
#: Written by `rfcindex.build`, read by the RFC tools to stamp their output.
#: They live here, with the `meta` table they describe, so the read path can
#: name them without importing the publisher-side package (which reaches the
#: network and must stay off the serve path).
META_SOURCE_MODEL = "vector_source_model"
META_SOURCE_BUILD = "rfc_index_build"
META_SOURCE_COMMIT = "rfc_index_commit"


@dataclass(frozen=True)
class VectorCodec:
    """How to read one index's vectors back into float32."""

    encoding: str = ENCODING_FLOAT32
    #: Only meaningful for int8: `float = int8 * scale`.
    scale: float = 1.0

    @property
    def itemsize(self) -> int:
        return 1 if self.encoding == ENCODING_INT8 else 4


#: The default, so a caller that hasn't looked at `meta` behaves exactly as
#: before this existed.
FLOAT32_CODEC = VectorCodec()


def read_codec(conn: sqlite3.Connection) -> VectorCodec:
    """The vector codec recorded in an index's `meta`, defaulting to float32."""
    # Via a cursor, not `conn.execute`: `search` reaches its connection through
    # a proxy that forwards `cursor` and `close` and nothing else, and keeping
    # that surface at two methods is worth more than the one saved line.
    try:
        rows = dict(
            conn.cursor()
            .execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?)",
                (_META_ENCODING, _META_SCALE),
            )
            .fetchall()
        )
    except sqlite3.Error:
        return FLOAT32_CODEC
    encoding = str(rows.get(_META_ENCODING) or ENCODING_FLOAT32)
    if encoding != ENCODING_INT8:
        return FLOAT32_CODEC
    try:
        scale = float(rows.get(_META_SCALE) or 0.0)
    except (TypeError, ValueError):
        scale = 0.0
    if scale <= 0:
        # An int8 index without a usable scale cannot be read at all; saying
        # so beats returning vectors scaled by a silent 1.0, which would look
        # like a catastrophic quality regression rather than a broken index.
        raise ValueError(
            "index declares int8 vectors but no positive vector_scale in meta"
        )
    return VectorCodec(encoding=ENCODING_INT8, scale=scale)


def write_codec(conn: sqlite3.Connection, codec: VectorCodec) -> None:
    """Record `codec` in `meta`. A float32 index writes nothing, so its meta
    stays byte-identical to one written before this existed."""
    if codec.encoding == ENCODING_FLOAT32:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        [(_META_ENCODING, codec.encoding), (_META_SCALE, repr(codec.scale))],
    )


def _pack(vec: Iterable[float]) -> bytes:
    """Pack and L2-normalise a vector for storage."""
    arr = np.asarray(list(vec), dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    return arr.tobytes()


def pack_vector(vec: Iterable[float], codec: VectorCodec) -> bytes:
    """Pack one vector under `codec`.

    The int8 path is here for symmetry and for tests: the one importer that
    writes int8 today copies vectors that are *already* quantised, byte for
    byte, which is the whole point of reusing them.
    """
    if codec.encoding != ENCODING_INT8:
        return _pack(vec)
    arr = np.asarray(list(vec), dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    quantised = np.rint(arr / np.float32(codec.scale)).clip(-127, 127)
    # `bytes(...)` rather than a bare `.tobytes()`: on Python 3.11's older
    # numpy stubs the latter infers as Any, which strict mypy rejects for a
    # function declared to return bytes. The runtime value is identical.
    return bytes(quantised.astype(np.int8).tobytes())


def _unpack_matrix(
    rows: List[bytes],
    codec: VectorCodec = FLOAT32_CODEC,
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Reshape a list of packed vectors into a single (n, dim) float32 matrix.

    int8 rows are dequantised but deliberately **not** re-normalised. The
    index that ships them was built that way, the quantisation error is
    reported by its own manifest as a mean cosine of 0.9998, and the
    retrieval numbers this was measured against were taken without
    re-normalising — so doing it here would make our scores disagree with
    the measurement for no accuracy that matters.

    Return type is annotated with full generic parameters because
    NumPy's stub on Python 3.10 (with newer numpy) treats bare
    `np.ndarray` as a generic-without-args error under strict mypy.
    The string form keeps it compatible across NumPy versions whose
    stubs treat the generic differently.
    """
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    dim = len(rows[0]) // codec.itemsize
    if codec.encoding == ENCODING_INT8:
        raw = np.frombuffer(b"".join(rows), dtype=np.int8).reshape(len(rows), dim)
        return np.asarray(raw, dtype=np.float32) * np.float32(codec.scale)
    return np.frombuffer(b"".join(rows), dtype=np.float32).reshape(len(rows), dim)


#: Clusters probed per query when an index carries an IVF partition. The
#: value upstream measured and publishes; 10→20 fixed four queries in 87,
#: 20→40 fixed one more for twice the bytes.
DEFAULT_NPROBE = 20

#: `meta` key overriding `DEFAULT_NPROBE` for one index.
META_NPROBE = "ivf_nprobe"


def load_centroids(
    conn: sqlite3.Connection, codec: VectorCodec
) -> "Optional[Tuple[np.ndarray[Any, np.dtype[np.float32]], List[int]]]":
    """The IVF centroids as `(matrix, ids)`, or None when the index has none.

    A gathered corpus has no partition — it is scanned whole, which is the
    right thing at a few tens of thousands of chunks — so absence is the
    ordinary case and not an error.
    """
    try:
        rows = (
            conn.cursor()
            .execute("SELECT id, vector FROM centroids ORDER BY id")
            .fetchall()
        )
    except sqlite3.Error:
        return None
    if not rows:
        return None
    ids = [int(r[0]) for r in rows]
    return _unpack_matrix([bytes(r[1]) for r in rows], codec), ids


#: Oldest on-disk schema a *read* will upgrade in place, rather than telling
#: the user to gather.
#:
#: From v9 upward every step is `ALTER TABLE ADD COLUMN` (plus one index) —
#: milliseconds, no data rewritten, no re-embedding. Below it the work is
#: real: v7 → v8 recreates the chunks table to widen a UNIQUE constraint, and
#: v8 → v9 hashes every row's text to backfill `chunk_hash`. Those belong to
#: an explicit gather, where the user is expecting to wait.
#:
#: Raise this in step with any future migration that does more than add a
#: nullable column.
_AUTO_UPGRADE_FROM = 9


def try_upgrade_schema(wg: str, current: int) -> bool:
    """Bring `wg`'s index up to the current schema for a *reader*.

    A schema bump would otherwise take `search_corpus` away from anyone who
    does not gather — the read path cannot migrate, so an index one release
    old is refused until its owner happens to run a gather, which a
    read-only MCP user may never do. That is a poor trade for two `ALTER
    TABLE`s.

    This is a narrow exception to "gather is the only writer", and stays
    narrow: it adds columns to a local index, never fetches, never changes a
    single chunk of content, and refuses to run at all when the step would be
    expensive (`_AUTO_UPGRADE_FROM`) or the index is not ours to write
    (`_index_immutable` — a published replica upgrades by being republished,
    not by a reader mutating a materialised copy).

    Returns True when the index is now current. Best-effort: any failure
    returns False and the caller falls back to telling the user to gather.
    """
    if current < _AUTO_UPGRADE_FROM or _index_immutable():
        return False
    path = _db_path_ro(wg)
    if not os.access(os.path.dirname(path) or ".", os.W_OK):
        return False
    try:
        # The same lock a build takes, so a reader upgrading and a gather
        # starting cannot both migrate at once.
        with file_lock(path + ".lock"):
            conn = _open_db(wg, path=path)
            conn.close()
    except (sqlite3.Error, OSError):
        return False
    return True


def read_meta(wg: str, keys: Iterable[str]) -> Dict[str, str]:
    """Selected `meta` values for a corpus, omitting any that are absent.

    Read-only and never migrates, so a reader can ask an index about itself
    without taking a write lock on it.
    """
    wanted = list(keys)
    if not wanted or not os.path.exists(_db_path_ro(wg)):
        return {}
    conn = _connect_ro(wg)
    try:
        placeholders = ",".join("?" * len(wanted))
        cur = conn.execute(
            f"SELECT key, value FROM meta WHERE key IN ({placeholders})", wanted
        )
        return {str(k): str(v) for k, v in cur.fetchall()}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def section_rows(wg: str, file: str, section: Optional[str]) -> List[Tuple[int, str]]:
    """`(chunk_idx, text)` for one section of one file, in document order.

    Rows store text with the chunker's carried-forward overlap trimmed off,
    so joining them reproduces the section as published. That is why a
    caller wanting a section reads this rather than one chunk: an individual
    row can begin mid-sentence (23% of trimmed rows do), and only the whole
    run is faithful.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return []
    conn = _connect_ro(wg)
    try:
        if section is None:
            cur = conn.execute(
                "SELECT chunk_idx, text FROM chunks "
                "WHERE file = ? AND section IS NULL ORDER BY chunk_idx",
                (file,),
            )
        else:
            cur = conn.execute(
                "SELECT chunk_idx, text FROM chunks "
                "WHERE file = ? AND section = ? ORDER BY chunk_idx",
                (file, section),
            )
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def section_outline(wg: str, file: str) -> List[Tuple[str, str, int]]:
    """`(section, title, characters)` per labelled section of `file`.

    Ordered by first appearance, which is document order, so the result
    reads as a table of contents rather than a lexical sort ("10" before
    "2").
    """
    if not os.path.exists(_db_path_ro(wg)):
        return []
    conn = _connect_ro(wg)
    try:
        cur = conn.execute(
            "SELECT section, title, sum(length(text)), min(chunk_idx) "
            "FROM chunks WHERE file = ? AND section IS NOT NULL "
            "GROUP BY section ORDER BY min(chunk_idx)",
            (file,),
        )
        return [(str(r[0]), str(r[1]), int(r[2] or 0)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def iter_sections(
    wg: str, files: Optional[Iterable[str]] = None
) -> Iterator[Tuple[str, Optional[str], str, str]]:
    """Yield `(file, section, title, text)` for every section of a corpus, in
    document order, each section's rows already joined.

    The whole-corpus counterpart to `section_rows`: for a reader that must look
    at *everything* rather than at one citation — a literal scan. Joining here
    rather than in the caller is the point, not a convenience. Rows carry the
    chunker's overlap trimmed off, so a phrase straddling a chunk boundary
    exists in neither row and is findable only in the joined run.

    Streams one section at a time: the RFC corpus is ~400 MB of text across
    ~233k sections, and a scan that materialised it would be a
    memory-exhaustion lever on a shared server.

    `files` restricts the scan to those corpus filenames. Sections with no
    label (~9% of the RFC series, mostly very old unnumbered documents) are
    yielded with `section=None` — one per chunk, since without a heading there
    is nothing claiming several chunks are one passage.

    Sections are cut as **consecutive runs** in document order rather than
    grouped by label, which keeps the SQL a single ordered pass and keeps the
    output in reading order. It relies on a section's chunks being contiguous,
    which holds for all but one `(file, section)` in the current RFC corpus;
    where it does not, that section is yielded as two runs. For a scan that
    degrades benignly — the text is all still read, one section is merely
    reported twice — whereas label-grouping would have to sort the whole
    corpus to recover the order.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return
    conn = _connect_ro(wg)
    try:
        sql = "SELECT file, section, title, text FROM chunks "
        params: List[Any] = []
        names = list(dict.fromkeys(files)) if files is not None else None
        if names is not None:
            if not names:
                return
            sql += f"WHERE file IN ({','.join('?' * len(names))}) "
            params = list(names)
        sql += "ORDER BY file, chunk_idx, sub_idx"
        cur = conn.execute(sql, params)
        key: Optional[Tuple[str, Optional[str]]] = None
        title = ""
        buf: List[str] = []
        for file, section, row_title, text in cur:
            here = (str(file), section)
            # An unlabelled chunk always starts its own run: consecutive NULLs
            # are separate passages, not one long one.
            if here != key or section is None:
                if key is not None:
                    yield (key[0], key[1], title, "\n".join(buf))
                key, title, buf = here, str(row_title or ""), []
            buf.append(str(text or ""))
        if key is not None:
            yield (key[0], key[1], title, "\n".join(buf))
    except sqlite3.Error:
        return
    finally:
        conn.close()


def indexed_files(wg: str) -> List[str]:
    """Every filename the corpus has chunks for, sorted.

    `chunk_counts` answers the same question but pays for a COUNT it does not
    need when the caller only wants the denominator of a scan.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return []
    conn = _connect_ro(wg)
    try:
        cur = conn.execute("SELECT DISTINCT file FROM chunks ORDER BY file")
        return [str(row[0]) for row in cur.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def chunk_counts(wg: str) -> Dict[str, int]:
    """Return {filename: chunk_count} for every file in the WG's index.

    Empty dict if no index exists yet. Used by tool_list_files so the
    consumer can see how many chunk_idx values are valid for each file
    instead of having to blind-probe.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return {}
    conn = _connect_ro(wg)
    try:
        # COUNT(DISTINCT chunk_idx): one logical chunk per message/window,
        # even when a long message spans several sub_idx fragments — so the
        # "0..N-1" hint stays the valid chunk_idx range for get_chunk_text.
        cur = conn.execute(
            "SELECT file, COUNT(DISTINCT chunk_idx) FROM chunks GROUP BY file"
        )
        return {str(row[0]): int(row[1]) for row in cur.fetchall()}
    finally:
        conn.close()


def chunk_spans(
    wg: str, files: Iterable[str]
) -> Dict[str, List[Tuple[int, int, int, str]]]:
    """Return {file: [(chunk_idx, start_line, end_line, title), ...]} for the
    named files, sorted by start_line.

    Lets a line-oriented reader (grep_corpus) map a matching line number back
    to the chunk that contains it, so a literal hit is citable — with the
    message title and a `chunk_idx` for `get_chunk_text` — without a second
    round-trip. Empty dict when no index exists; files with no indexed chunks,
    and chunks indexed before line tracking (schema v1, NULL spans), are simply
    absent, so callers must treat attribution as best-effort.
    """
    names = list(dict.fromkeys(files))
    if not names:
        return {}
    if not os.path.exists(_db_path_ro(wg)):
        return {}
    conn = _connect_ro(wg)
    try:
        out: Dict[str, List[Tuple[int, int, int, str]]] = {}
        # Chunked IN-list: a grep hit set can name more files than SQLite's
        # default 999-variable limit allows in one statement.
        for i in range(0, len(names), 500):
            batch = names[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                "SELECT file, chunk_idx, start_line, end_line, title FROM chunks "
                f"WHERE sub_idx=0 AND start_line IS NOT NULL AND file IN ({placeholders})",
                batch,
            )
            for row in cur.fetchall():
                out.setdefault(str(row[0]), []).append(
                    (
                        int(row[1]),
                        int(row[2]),
                        int(row[3]) if row[3] is not None else int(row[2]),
                        _clean_title(str(row[4])),
                    )
                )
        for spans in out.values():
            spans.sort(key=lambda span: span[1])
        return out
    finally:
        conn.close()


def _citation_url_variants(url: str) -> List[str]:
    """Equivalent spellings of a citation URL, for tolerant matching.

    A message body cites an archive permalink in whatever form a mail
    client produced — with or without a trailing slash, `http` vs
    `https`, a leading `www.`, angle-bracket wrapping, or a trailing
    `#fragment`. The `url` column stores one canonical form per
    message, so a bare `WHERE url = ?` misses a footnote that differs
    only in those incidentals (a lone trailing slash caused a real
    "not in the corpus" miss). Return the small set of forms to match.

    This bridges *within-scheme* variance only. It deliberately does
    not map a `mailarchive.ietf.org/arch/msg/<token>` permalink to a
    `www.w3.org/mid/<message-id>` one (or vice versa): the token is an
    opaque hash and the mid is the RFC 5322 Message-ID, so the two are
    not string-convertible without an identity map we do not hold.
    """
    text = url.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        # Not a decomposable URL — match it verbatim.
        return [text]
    hosts = {parts.netloc}
    if parts.netloc.startswith("www."):
        hosts.add(parts.netloc[4:])
    else:
        hosts.add("www." + parts.netloc)
    paths = {parts.path}
    if parts.path.endswith("/"):
        paths.add(parts.path.rstrip("/") or "/")
    elif parts.path:
        paths.add(parts.path + "/")
    variants = set()
    for scheme in ("http", "https"):
        for host in hosts:
            for path in paths:
                # Fragment dropped (last arg ""); query preserved.
                variants.add(urlunsplit((scheme, host, path, parts.query, "")))
    return sorted(variants)


def find_chunks_by_url(
    wg: str, url: str
) -> List[Tuple[str, int, str, str, Optional[int], Optional[int]]]:
    """All chunks whose `url` matches the given citation URL (modulo the
    incidental spelling differences in `_citation_url_variants`), sorted
    by (file, chunk_idx).

    Returns an empty list if no chunk matches. A thread Archived-At URL
    is per-message and matches exactly one chunk; a GitHub issue URL
    is file-level and matches every chunk in that issue's per-issue
    file. Callers (notably the MCP `get_by_url` tool) use the row
    count to decide whether to return a single chunk or the whole file.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return []
    conn = _connect_ro(wg)
    try:
        # sub_idx 0 only: it carries the full message text and span, so a
        # split message resolves to one row here (not one per fragment),
        # keeping the single-vs-file-level distinction the caller makes on
        # the row count intact. A pre-v8 read-only cache lacks the column
        # (read-only opens don't migrate) — skip the clause there rather
        # than crash; the worst case is a split message matching as
        # several rows until the next `--rebuild-embeddings`.
        have = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        # A genuinely pre-v6 cache has no `url` column at all — there is
        # nothing to resolve against, so miss gracefully (the tool's
        # "index may predate the url column" message then reads true)
        # rather than raise OperationalError on the WHERE clause.
        if "url" not in have:
            return []
        sub_idx_clause = " AND sub_idx = 0" if "sub_idx" in have else ""
        variants = _citation_url_variants(url)
        placeholders = ",".join("?" * len(variants))
        cur = conn.execute(
            "SELECT file, chunk_idx, title, text, start_line, end_line "
            f"FROM chunks WHERE url IN ({placeholders}){sub_idx_clause} "
            "ORDER BY file, chunk_idx",
            variants,
        )
        out: List[Tuple[str, int, str, str, Optional[int], Optional[int]]] = []
        for row in cur.fetchall():
            out.append(
                (
                    str(row[0]),
                    int(row[1]),
                    _clean_title(str(row[2])),
                    str(row[3]),
                    int(row[4]) if row[4] is not None else None,
                    int(row[5]) if row[5] is not None else None,
                )
            )
        return out
    finally:
        conn.close()


def get_messages(
    wg: str, items: Iterable[Tuple[str, int]]
) -> Dict[Tuple[str, int], Tuple[str, str, Optional[str], Optional[str]]]:
    """Batch fetch (title, text, chunk_date, url) for a list of
    (file, chunk_idx) pairs.

    Returns a dict keyed by (file, chunk_idx); missing rows are simply
    absent from the result. Used by `tool_read_topic` to render a
    chronological topic timeline without one SQL round-trip per message.
    """
    keys = list(items)
    if not keys:
        return {}
    if not os.path.exists(_db_path_ro(wg)):
        return {}
    conn = _connect_ro(wg)
    try:
        # SQLite has no native (a,b) IN ((...),(...)) shortcut for many
        # pairs, but the chunks table is small enough that a single
        # SELECT with a per-pair WHERE OR chain is fine — and we cap
        # the caller (k≤40-ish) so the IN-list never explodes.
        clauses = " OR ".join(["(file=? AND chunk_idx=?)"] * len(keys))
        args: List[object] = []
        for file, idx in keys:
            args.extend([file, idx])
        # sub_idx=0 holds the full message body and span, so each
        # (file, chunk_idx) yields exactly one row — the whole message,
        # not a fragment.
        cur = conn.execute(
            "SELECT file, chunk_idx, title, text, chunk_date, url "
            f"FROM chunks WHERE sub_idx=0 AND ({clauses})",
            args,
        )
        out: Dict[Tuple[str, int], Tuple[str, str, Optional[str], Optional[str]]] = {}
        for row in cur.fetchall():
            out[(str(row[0]), int(row[1]))] = (
                _clean_title(str(row[2])),
                str(row[3]),
                str(row[4]) if row[4] is not None else None,
                str(row[5]) if row[5] is not None else None,
            )
        return out
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
    if not os.path.exists(_db_path_ro(wg)):
        return None
    conn = _connect_ro(wg)
    try:
        # sub_idx 0 carries the full message text and the section's full
        # line span; later fragments are embedding-only slices. Return the
        # whole message so get_chunk_text shows it intact.
        cur = conn.execute(
            "SELECT title, text, start_line, end_line FROM chunks "
            "WHERE file=? AND chunk_idx=? ORDER BY sub_idx LIMIT 1",
            (file, chunk_idx),
        )
        row = cur.fetchone()
        if not row:
            return None
        start_line = int(row[2]) if row[2] is not None else None
        end_line = int(row[3]) if row[3] is not None else None
        return (_clean_title(str(row[0])), str(row[1]), start_line, end_line)
    finally:
        conn.close()


def any_indexed_wg() -> Optional[str]:
    """Name of some corpus that has a built index, or None.

    Scans the index dir for a ``<wg>/embeddings.db``. Used by the server's
    readiness probe to pick a real index to open.
    """
    root = get_index_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        if os.path.exists(os.path.join(root, name, "embeddings.db")):
            return name
    return None


def probe_index(wg: str) -> bool:
    """Open the WG index read-only and run a trivial read.

    True if it opens and reads; False on any sqlite / OS error. The
    readiness probe uses this to catch an index that exists on disk but
    cannot actually be served -- e.g. a WAL-mode DB on a read-only mount
    (needs ``IETF_LLM_INDEX_IMMUTABLE``), or a truncated / corrupt file --
    which a bare directory stat would miss.
    """
    try:
        conn = _connect_ro(wg)
        try:
            conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return True
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


# --- document-level vectors + topic-map sidecar (issue #116) ---------------

#: Cap on the text carried per document into the topic-map term-labelling
#: pass, so one long thread can't dominate the tf-idf vocabulary. The first
#: few KB of a thread/issue/draft is plenty to characterise its theme.
_DOC_TEXT_CAP = 4000

#: Title the chunker gives a thread/issue's leading metadata section (see
#: `chunking._emit_section`). Its body is the subject `# H1` plus span /
#: participants / outline — useful for the document title, but pure noise
#: (and a systematic injector of participant names) for topical term
#: labelling, so `load_documents` mines its subject and drops the rest.
_HEADER_TITLE = "(thread header)"


@dataclass
class Document:
    """One source file reduced to a single representative vector, for the
    document-level clustering the topic map (and centroid routing) run over.

    `vector` is the L2-normalised mean of the file's chunk vectors — clustering
    over per-file centroids, not raw chunks, keeps a 200-chunk megathread from
    dominating the themes (issue #116). `title` is the file's first chunk title
    (the thread subject / issue title), `text` a capped sample for term
    labelling, `last_active` the newest chunk date (None if the file is undated).
    """

    file: str
    title: str
    text: str
    last_active: Optional[str]
    vector: "np.ndarray[Any, np.dtype[np.float32]]"


def load_documents(wg: str) -> List[Document]:
    """Return one `Document` per indexed file: its chunk vectors mean-pooled
    and renormalised, plus the metadata the topic map needs to label clusters.

    Empty list when there is no index. Read-only; never migrates the DB.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return []
    conn = _connect_ro(wg)
    try:
        codec = read_codec(conn)
        cur = conn.execute(
            "SELECT file, chunk_idx, sub_idx, title, text, embedding, chunk_date "
            "FROM chunks ORDER BY file, chunk_idx, sub_idx"
        )
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if not rows:
        return []

    # Group rows by file, preserving the (chunk_idx, sub_idx) order the query
    # imposed so the first row of each file is its representative title.
    by_file: Dict[str, List[Any]] = {}
    for row in rows:
        by_file.setdefault(str(row[0]), []).append(row)

    docs: List[Document] = []
    for file, frows in by_file.items():
        vecs = _unpack_matrix([r[5] for r in frows], codec)
        pooled = vecs.mean(axis=0)
        norm = float(np.linalg.norm(pooled))
        if norm:
            pooled = (pooled / norm).astype(np.float32)
        # Representative title and term text. A thread/issue leads with a
        # metadata section: mine its `# subject` H1 for the title and drop the
        # rest (the participants line would otherwise inject every name into
        # the tf-idf vocabulary). A draft/RFC has no such header — its filename
        # stem (the document name) is the better title than its boilerplate
        # first line.
        header_subject: Optional[str] = None
        text_parts: List[str] = []
        budget = _DOC_TEXT_CAP
        for row in frows:
            chunk_title = _clean_title(str(row[3]))
            body = str(row[4])
            if chunk_title == _HEADER_TITLE:
                if header_subject is None:
                    first = body.split("\n", 1)[0].strip()
                    if first.startswith("#"):
                        header_subject = first.lstrip("#").strip()
                continue
            if budget > 0:
                piece = body[:budget]
                text_parts.append(piece)
                budget -= len(piece)
        title = header_subject or os.path.splitext(os.path.basename(file))[0]
        dates = [str(r[6]) for r in frows if r[6] is not None]
        last_active = max(dates) if dates else None
        docs.append(
            Document(
                file=file,
                title=title,
                text=" ".join(text_parts),
                last_active=last_active,
                vector=pooled.astype(np.float32),
            )
        )
    return docs


def encode_centroid(vec: "np.ndarray[Any, np.dtype[np.float32]]") -> str:
    """Base64-encode a centroid as packed float32 — exact and ~3-4x smaller
    than a JSON decimal array. Mirrors `_pack`'s on-disk representation; decode
    with `decode_centroid`."""
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def decode_centroid(blob: str) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Inverse of `encode_centroid`: a 1-D float32 vector."""
    return np.frombuffer(base64.b64decode(blob), dtype=np.float32)


def _topics_path(wg: str, *, write: bool) -> str:
    """Path to `wg`'s topic-map sidecar, beside `embeddings.db`. The write
    path uses the local index dir (`build_index`'s home); the read path
    resolves through the corpus store so a cloud replica reads the published
    version's sidecar."""
    db = _db_path(wg) if write else _db_path_ro(wg)
    return os.path.join(os.path.dirname(db), "topics.json")


def write_topics(wg: str, payload: Dict[str, Any]) -> None:
    """Write the topic-map sidecar atomically (temp + rename) beside the
    index, so a reader never sees a half-written file."""
    path = _topics_path(wg, write=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
    os.replace(tmp, path)


def read_topics(wg: str) -> Optional[Dict[str, Any]]:
    """Return the parsed topic-map sidecar, or None if absent / unreadable.
    Read-only; a corpus indexed before the topic map shipped simply has none."""
    path = _topics_path(wg, write=False)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None
