"""Assemble an `embeddings.db` from rfc.fyi's published index and a mirror.

This is where the two halves meet: the index supplies a vector and a byte
range per chunk, the reconciled mirror supplies the bytes, and the result is
an ordinary ietf-llm index that `search` reads with no special casing.

**One row per chunk, not per section.** Each chunk has its own vector, and
retrieval quality depends on that: a 629-chunk section pooled into a single
vector would be mush. Sections are recovered by grouping rows, not by
storing them.

**Text is stored trimmed.** rfc.fyi's chunker carries a trailing paragraph
(up to 500 characters) into the next chunk, so consecutive chunks in a
section overlap. Each row stores only the part of its range that no earlier
chunk already covers, which makes a section exactly the concatenation of its
rows in order — no de-duplication at read time, and no duplicated prose in
the corpus. Measured over the series, storing the section text on every one
of its chunks instead would cost 3,875 MiB against 434 MiB.

That concatenation is not quite byte-identical to cleaning the whole section
at once: a paragraph split across a page break that falls exactly on a chunk
boundary gets rejoined in the second case and not the first. Measured on
2,942 multi-chunk sections, 2,938 agree and 4 differ by at most 92
characters. Worth knowing; not worth a second copy of the corpus to fix.

**`chunk_hash` is left NULL.** It exists so a re-gather can reuse the vector
for an unchanged chunk. This corpus is never gathered — it is re-seeded
wholesale when upstream rebuilds — so the column would never be read, and at
64 hex characters of high-entropy text per row it costs 28 MiB on disk and
34 MiB in the bundle, which is the single largest saving available here.
NULL is an already-supported state for it (windowed chunks have always been
written that way).

**`model` records the model to embed *queries* with, not the one that
produced the vectors.** `search` uses that field to construct an embedder,
so it has to name something our loader can build. The producing id is kept
beside it as `vector_source_model`, so nothing is falsified — the two are
the same weights reached through different runtimes, which is exactly the
equivalence issue #230 tracks as an open decision for the seed store.

Publisher-side only — see the subpackage docstring.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..embeddings.storage import (
    DEFAULT_NPROBE,
    ENCODING_INT8,
    META_NPROBE,
    META_SOURCE_BUILD,
    META_SOURCE_COMMIT,
    META_SOURCE_MODEL,
    VectorCodec,
    _open_db,
    write_codec,
)
from ..log import LogLevel, Verbosity, log
from .format import (
    ChunkMeta,
    IndexManifest,
    iter_clusters,
    read_centroids,
    read_manifest,
    read_sources,
)
from .mirror import reconcile, text_path
from .text import clean_section_text

#: The model a *query* must be embedded with to score these vectors. Not the
#: id that produced them (see the module docstring); that is recorded as
#: provenance instead.
QUERY_MODEL = "sentence-transformers/BAAI/bge-small-en-v1.5"

#: Chunker identity for the seed store's compatibility tuple. Stable across
#: monthly rebuilds on purpose: it names *whose* chunker shaped the corpus,
#: not which build. Tying it to the build id would make every refresh look
#: incompatible, and this member has no cold-gather path to fall back to.
#: Bump by hand if rfc.fyi's chunking changes materially.
CHUNKER_ID = "rfcfyi-1"

_TEXT_BASE = "https://www.rfc-editor.org/rfc"

#: Rows per executemany. Large enough that the write is not per-row overhead,
#: small enough that peak memory stays flat over 457k chunks.
_WRITE_BATCH = 2000

#: Page size for the finished corpus. A row here averages ~1,422 bytes of
#: payload against SQLite's ~1,000-byte max-local limit on the default 4 KiB
#: page, so almost every row spills to an overflow page and the table packs
#: 2.2 rows per page. At 16 KiB the payload fits inline: 805 MiB becomes 683
#: MiB on disk. It barely moves the *bundle* (304 MiB gzipped becomes 297,
#: since page slack compresses away) — this is a disk win, not a download one.
_PAGE_SIZE = 16384


@dataclass
class BuildStats:
    """What a build actually wrote, for the publisher to log."""

    rfcs: int = 0
    chunks: int = 0
    sections: int = 0
    #: RFCs the index describes but the mirror could not confirm — a reissue
    #: or an absent file. Their chunks are dropped rather than mis-joined.
    skipped_rfcs: List[str] = field(default_factory=list)
    #: Chunks whose range cleaned to nothing (RFC 635 has one whose whole
    #: extent is a running header) or which an earlier chunk fully covered.
    empty_chunks: int = 0
    text_bytes: int = 0

    def summary(self) -> str:
        parts = [
            f"{self.chunks:,} chunks from {self.rfcs:,} RFCs",
            f"{self.sections:,} sections",
            f"{self.text_bytes / 1048576:.0f} MiB of text",
        ]
        if self.skipped_rfcs:
            parts.append(f"{len(self.skipped_rfcs):,} RFCs skipped")
        if self.empty_chunks:
            parts.append(f"{self.empty_chunks:,} empty chunks dropped")
        return "; ".join(parts)


def _trimmed_ranges(chunks: List[ChunkMeta]) -> List[Tuple[ChunkMeta, int, int]]:
    """Order a section's chunks and clip each to the part not already covered.

    The chunker's carried-forward paragraph is what makes this necessary; see
    the module docstring. A chunk an earlier one fully covers yields an empty
    range and is dropped by the caller.
    """
    out: List[Tuple[ChunkMeta, int, int]] = []
    covered = 0
    for chunk in sorted(chunks, key=lambda c: (c.off, c.length)):
        start = max(chunk.off, covered)
        end = chunk.off + chunk.length
        covered = max(covered, end)
        out.append((chunk, start, end))
    return out


def _rows_for_rfc(
    rfc: str,
    chunks: List[Tuple[ChunkMeta, bytes, int]],
    mirror_dir: str,
) -> Tuple[List[Tuple[Any, ...]], int, int]:
    """Build the insert rows for one RFC. Returns `(rows, sections, empties)`."""
    path = text_path(mirror_dir, rfc)
    with open(path, "rb") as handle:
        raw = handle.read()

    vectors = {id(meta): blob for meta, blob, _cid in chunks}
    clusters = {id(meta): cid for meta, _blob, cid in chunks}
    by_section: Dict[Optional[str], List[ChunkMeta]] = defaultdict(list)
    for meta, _blob, _cid in chunks:
        by_section[meta.section].append(meta)

    file = f"rfc{rfc}.txt"
    url = f"{_TEXT_BASE}/{file}"
    prepared: List[Tuple[int, Tuple[Any, ...]]] = []
    empties = 0
    for section, members in by_section.items():
        for meta, start, end in _trimmed_ranges(members):
            text = (
                clean_section_text(raw[start:end].decode("utf-8", errors="replace"))
                if start < end
                else ""
            )
            if not text.strip():
                empties += 1
                continue
            prepared.append(
                (
                    meta.off,
                    (
                        file,
                        0,  # chunk_idx, assigned below in document order
                        0,  # sub_idx
                        meta.title,
                        text,
                        vectors[id(meta)],
                        url,
                        section,
                        clusters[id(meta)],
                    ),
                )
            )

    # chunk_idx is an ordinal within the file, and callers expect document
    # order (`get_chunk_text` reads a consecutive range), so it is assigned
    # after grouping rather than per section.
    prepared.sort(key=lambda item: item[0])
    rows = [
        (row[0], idx, row[2], row[3], row[4], row[5], row[6], row[7], row[8])
        for idx, (_off, row) in enumerate(prepared)
    ]
    return rows, len([s for s in by_section if s is not None]), empties


def build_rfc_index(  # pylint: disable=too-many-locals
    index_dir: str,
    mirror_dir: str,
    db_path: str,
    manifest: Optional[IndexManifest] = None,
    verbosity: Verbosity = Verbosity.STATUS,
) -> BuildStats:
    """Write `db_path` from the published index at `index_dir` and `mirror_dir`.

    RFCs whose mirrored bytes do not match the digests the index shipped are
    skipped entirely: their offsets describe text we do not have, and a
    partial corpus is a great deal better than a mis-attributed one.
    """
    manifest = manifest or read_manifest(index_dir)
    usable = _usable_rfcs(index_dir, mirror_dir)

    grouped: Dict[str, List[Tuple[ChunkMeta, bytes, int]]] = defaultdict(list)
    dropped: Set[str] = set()
    for cluster in iter_clusters(index_dir, manifest):
        for row, meta in enumerate(cluster.chunks):
            if usable is not None and meta.rfc not in usable:
                # Its bytes moved since the build, or it is not in the mirror.
                # Collected as we go: a set difference afterwards cannot see
                # these, because they never enter `grouped` at all.
                dropped.add(meta.rfc)
                continue
            grouped[meta.rfc].append(
                (meta, cluster.vectors[row].tobytes(), cluster.ident)
            )
    stats = BuildStats(skipped_rfcs=sorted(dropped, key=_rfc_sort_key))
    conn = _open_db("", path=db_path)
    try:
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM centroids")
        write_codec(conn, VectorCodec(ENCODING_INT8, manifest.scale))
        _write_meta(conn, manifest)
        _write_centroids(conn, index_dir, manifest)
        batch: List[Tuple[Any, ...]] = []
        for rfc in sorted(grouped, key=_rfc_sort_key):
            if not os.path.isfile(text_path(mirror_dir, rfc)):
                stats.skipped_rfcs.append(rfc)
                continue
            rows, sections, empties = _rows_for_rfc(rfc, grouped[rfc], mirror_dir)
            stats.rfcs += 1
            stats.sections += sections
            stats.empty_chunks += empties
            stats.chunks += len(rows)
            stats.text_bytes += sum(len(str(r[4])) for r in rows)
            batch.extend(rows)
            if len(batch) >= _WRITE_BATCH:
                _flush(conn, batch)
                batch = []
        _flush(conn, batch)
        conn.commit()
    finally:
        conn.close()
    _compact(db_path)
    log(f"rfc index: {stats.summary()}", verbosity, LogLevel.STATUS)
    return stats


def _compact(db_path: str) -> None:
    """Rewrite the finished corpus at `_PAGE_SIZE`.

    Done at the end rather than at creation because the page size only takes
    effect on an empty database, and VACUUM is the supported way to change it
    afterwards. A second or two, and it leaves no free pages behind.

    Journal mode goes to DELETE first: SQLite silently ignores a page-size
    change while a database is in WAL, so without this the VACUUM runs and
    achieves nothing. Leaving DELETE is also what a finished index wants —
    it is a single file with no `-wal`/`-shm` sidecars, which is the same
    reason `promote_build_db` relies on for its atomic swap, and the only
    form that can be bundled.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(f"PRAGMA page_size={_PAGE_SIZE}")
        conn.execute("VACUUM")
    finally:
        conn.close()


def _write_centroids(
    conn: sqlite3.Connection, index_dir: str, manifest: IndexManifest
) -> None:
    """Copy the IVF partition's centroids into the corpus.

    A table rather than a file beside the database: it then travels wherever
    the index does — the seed bundle already ships `embeddings.db` and nothing
    else — with no new path to resolve and no way for the two to arrive out of
    step. 4,337 rows at 384 int8 dimensions is 1.6 MiB, against the ~160 ms a
    query saves by not reading the other 455,000 vectors.
    """
    matrix = read_centroids(index_dir, manifest)
    conn.executemany(
        "INSERT INTO centroids(id, vector) VALUES(?, ?)",
        [(i, matrix[i].tobytes()) for i in range(matrix.shape[0])],
    )


def _flush(conn: sqlite3.Connection, batch: List[Tuple[Any, ...]]) -> None:
    if not batch:
        return
    conn.executemany(
        "INSERT INTO chunks(file, chunk_idx, sub_idx, title, text, embedding, "
        "url, section, cluster_id) VALUES(?,?,?,?,?,?,?,?,?)",
        batch,
    )


def _usable_rfcs(index_dir: str, mirror_dir: str) -> Optional[Set[str]]:
    """RFC numbers whose mirrored bytes match the build, or None when the
    index shipped no digests to check against."""
    digests = read_sources(index_dir)
    if not digests:
        return None
    result = reconcile(mirror_dir, digests)
    unusable = set(result.differing) | set(result.absent)
    return {rfc for rfc in digests if rfc not in unusable}


def _rfc_sort_key(rfc: str) -> Tuple[int, str]:
    """Numeric where possible; the two `"17a"` chunks sort beside RFC 17."""
    digits = "".join(c for c in rfc if c.isdigit())
    return (int(digits) if digits else 0, rfc)


def _write_meta(conn: sqlite3.Connection, manifest: IndexManifest) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        [
            ("model", QUERY_MODEL),
            ("chunker_version", CHUNKER_ID),
            ("embed_dim", str(manifest.dims)),
            (META_SOURCE_MODEL, manifest.model_id),
            (META_SOURCE_BUILD, manifest.build),
            (META_SOURCE_COMMIT, manifest.source_commit),
            (META_NPROBE, str(manifest.nprobe or DEFAULT_NPROBE)),
        ],
    )
