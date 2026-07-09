# pylint: disable=too-many-lines
"""Build the per-WG embedding index, and query it.

`build_index(wg, cache_dir, ...)` walks the cache, chunks each eligible
file, embeds the chunks, and stores them in the WG's sqlite DB. The
operation is incremental: a file whose content hash matches the one
recorded at its last embed is skipped. Keying on content (not mtime)
keeps the skip stable across hosts -- a cloud replica that materialises
a published version onto fresh local files recognises the identical
bytes as already-embedded, so it doesn't re-embed the whole corpus.

`search(wg, query, ...)` reads back every stored embedding, computes
cosine similarity against a freshly-embedded query (single numpy
matmul; vectors were stored normalised), and returns the top-k hits.
"""

from __future__ import annotations

import gc
import hashlib
import os
import sqlite3
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from ..atomicio import file_lock
from ..log import LogLevel, Verbosity, log
from .chunking import CHUNKER_VERSION, _chunk_file, _eligible_files
from .models import (
    DEFAULT_EMBED_MODEL,
    _get_embed_model,
    embed_concurrency,
    is_remote_embed_model,
)
from .snippet import make_snippet
from .storage import (
    _SCHEMA_VERSION,
    _connect_ro,
    _db_path,
    _db_path_ro,
    _open_db,
    _pack,
    _unpack_matrix,
    chunk_hash,
    discard_build_db,
    promote_build_db,
    seed_build_db,
)

#: Emit an embed-progress update (STATUS log + gather_status detail) at most
#: this often. Short enough that a slow embed doesn't look like the gather has
#: hung, long enough that small WGs don't get spammed.
_PROGRESS_SECS = 20.0
#: Commit the in-flight transaction (and evict the allocator cache) on a dual
#: cadence: after this many chunks written since the last flush, OR after
#: `_FLUSH_EVERY_FILES` files — whichever comes first.
#:
#: This bounds the WRITE phase: it caps the in-flight SQLite transaction / WAL
#: size and is a durability floor (a crash discards at most ~this many chunks'
#: writes). MPS peak during embedding is bounded separately, by `_EMBED_BUFFER`
#: (evicted per window), so the eviction here is now just cleanup. The value is
#: inherited from when embed and write were interleaved per file and this
#: trigger *was* the MPS bound: on the httpbis rebuild the dense-draft region
#: cost ~4 MB of driver memory per chunk above a ~2 GB floor, so 500 held the
#: peak near ~4 GB — and it stays a safe write-batch size regardless.
_FLUSH_EVERY_CHUNKS = 500
#: The file-count trigger is a durability floor for the opposite regime — a
#: long run of small/sparse files (threads, issues) that never reaches the
#: chunk threshold still commits periodically, so a crash doesn't discard much
#: and the on-disk WAL stays bounded.
_FLUSH_EVERY_FILES = 25
#: On-device path: how many chunks to buffer across files before an embed
#: flush. Chunks are pooled to ~this many and embedded in one batched call, so
#: sentence-transformers gets a wide window to length-sort internally — tight
#: padding and few calls, most of the speed of sorting the whole corpus but
#: with bounded memory: only ~this many chunks (plus the few files feeding
#: them) are ever resident, and each flush commits, so the build stays
#: streaming and crash-durable. Also the progress cadence. 512 gives ST a deep
#: sort window while keeping each flush short.
_EMBED_BUFFER = 512


def _mps_empty_cache() -> Optional[Callable[[], None]]:
    """Return torch's MPS `empty_cache`, or None when MPS isn't in play.

    MPS is opt-in now (`IETF_LLM_EMBED_DEVICE=mps`; the default is CPU — see
    `models._embed_device`), but on that path the MPS caching allocator holds
    freed blocks and forward passes leak a little, so a long build's high-water
    mark climbs — and because torch's default `PYTORCH_MPS_HIGH_WATERMARK_RATIO`
    (1.7) only errors *above* physical RAM, an overrun thrashes swap rather than
    raising. The on-device build evicts per window (`_flush_window`) to cap it.

    torch is imported lazily (only when a build actually runs) so the CLI and
    the torch-free remote-embedding path pay nothing. Returns None on CPU/CUDA
    or when torch is absent (or lacks MPS), so callers no-op transparently.
    """
    try:
        # pylint: disable=import-outside-toplevel,import-error
        import torch  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return None
    mps = getattr(torch, "mps", None)
    try:
        available = mps is not None and torch.backends.mps.is_available()
    except (AttributeError, RuntimeError):
        available = False
    if not available:
        return None
    return getattr(mps, "empty_cache", None)


@dataclass
class Hit:
    score: float
    file: str
    chunk_idx: int
    title: str
    snippet: str
    # 1-indexed inclusive line range within `file`. May be None for
    # chunks indexed before line tracking was added (schema v1) until
    # the user runs `--rebuild-embeddings`.
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    # Comma-separated lowercased GitHub labels, only set for chunks
    # from per-issue files. None for thread/draft/transcript chunks.
    # Surfaced in the search output so the caller can see at-a-glance
    # why an issue chunk matched a topical query.
    labels: Optional[str] = None
    # Normalised issue state ('open' / 'closed') for issue chunks; None
    # everywhere else. Helps callers prefer the chairs' resolution over
    # older mid-debate threads.
    state: Optional[str] = None
    # Citation URL for the chunk's source — a GitHub issue URL for
    # issue chunks, an IETF Archived-At permalink for thread message
    # chunks, None for drafts/transcripts. Surfaced in MCP search
    # output so a citing LLM doesn't have to reconstruct it.
    url: Optional[str] = None
    # Issue-cluster signals (issue chunks only). `duplicate_of` is the
    # #N this issue is marked as a dup of (file-level); the consuming
    # LLM can skip reading dup issues. `closing_rationale` is the last
    # comment on a closed issue, useful as a one-line "why" indicator.
    duplicate_of: Optional[int] = None
    closing_rationale: Optional[str] = None


def _file_hash(path: str) -> Optional[str]:
    """SHA-256 hex digest of a file's bytes, or None if it can't be read.

    The incremental-rebuild key: a file whose hash matches the value stored at
    its last embed is unchanged and skipped. Content-based (not mtime) so the
    skip survives a cross-host copy -- a cloud replica materialises a published
    version onto fresh local files (new mtimes, identical bytes) and still
    recognises them as already-embedded. Streamed so a large draft doesn't load
    whole into memory."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


@dataclass
class _FilePlan:
    """One changed file's embedding work, prepared on the main thread (DB
    skip-check + chunking + per-chunk reuse) so only the network embed runs
    off-thread.

    `chunks` is the file's full new chunk set, in order. `reused[i]` is the
    stored embedding blob to keep for `chunks[i]` (its embedded text is unchanged
    since last build), or None when the chunk must be embedded. `texts` holds only
    the to-embed texts, in `chunks` order; `hashes[i]` is `chunks[i]`'s content
    hash, stored on write for the next build's reuse."""

    relpath: str
    hash_key: str
    cur_hash: Optional[str]
    chunks: List[Any]
    texts: List[str]
    reused: List[Optional[bytes]]
    hashes: List[str]


def _embed_text_of(chunk: Any) -> str:
    """The exact text embedded for a chunk: a split section's `sub_idx 0` sets
    `embed_text` to just its first window; everything else embeds its full
    `text`."""
    return str(chunk.embed_text if chunk.embed_text is not None else chunk.text)


def _plan_file(
    cur: sqlite3.Cursor,
    path: str,
    cache_dir: str,
    file_hashes: Dict[str, Optional[str]],
    already: "set[str]",
) -> Optional[_FilePlan]:
    """Skip-check (unchanged content already indexed) and chunk one file.

    Returns a `_FilePlan` for a file that needs (re-)embedding, or None to
    skip. Runs on the main thread — it reads the cursor and the chunker, but
    never embeds, so the slow network call can be dispatched off-thread.

    For a changed file, diffs the new chunks against the stored ones by content
    hash (issue #183): a chunk whose embedded text is unchanged reuses its stored
    vector, so only genuinely new/changed chunks are embedded."""
    relpath = os.path.relpath(path, cache_dir)
    hash_key = f"hash:{relpath}"
    cur_hash = file_hashes[relpath]
    cur.execute("SELECT value FROM meta WHERE key=?", (hash_key,))
    prev = cur.fetchone()
    if relpath in already and prev and cur_hash is not None and prev[0] == cur_hash:
        return None  # unchanged
    chunks = _chunk_file(path, relpath)
    if not chunks:
        return None
    # Stored vectors keyed by their content hash, for reuse. NULL hashes (legacy
    # rows not yet backfilled, or windowed chunks) don't participate.
    reuse: Dict[str, bytes] = {}
    for stored_hash, embedding in cur.execute(
        "SELECT chunk_hash, embedding FROM chunks WHERE file=?", (relpath,)
    ):
        if stored_hash is not None and stored_hash not in reuse:
            reuse[stored_hash] = embedding
    hashes = [chunk_hash(_embed_text_of(c)) for c in chunks]
    reused: List[Optional[bytes]] = [reuse.get(h) for h in hashes]
    texts = [_embed_text_of(c) for c, r in zip(chunks, reused) if r is None]
    return _FilePlan(relpath, hash_key, cur_hash, chunks, texts, reused, hashes)


def _write_file(
    cur: sqlite3.Cursor, plan: _FilePlan, vectors: List[Any], verbose: Verbosity
) -> int:
    """Replace one file's chunk rows with the freshly embedded set and stamp
    its content hash. Returns chunks written, or 0 when the file is skipped
    (a vector/chunk count mismatch or a duplicate key). DB-only and
    main-thread; the caller owns commit cadence and progress."""
    n_embed = sum(1 for r in plan.reused if r is None)
    if len(vectors) != n_embed:
        # A short vector list would silently drop the trailing chunks (zip
        # stops at the shorter sequence) while a hash stamp would mark the file
        # fully indexed. Skip instead, so it is retried next run.
        log(
            f"Embedding returned {len(vectors)} vectors for {n_embed} to-embed "
            f"chunks in {plan.relpath}; skipping (retried next run).",
            verbose,
            level=LogLevel.ERROR,
        )
        return 0
    fresh = iter(vectors)
    try:
        # Drop any stale chunks for this file, then insert the new set — each
        # chunk carrying either its reused stored vector or a freshly embedded
        # one, plus its content hash for the next build's reuse (issue #183).
        cur.execute("DELETE FROM chunks WHERE file=?", (plan.relpath,))
        for chunk, reuse, chash in zip(plan.chunks, plan.reused, plan.hashes):
            embedding = reuse if reuse is not None else _pack(next(fresh))
            cur.execute(
                "INSERT INTO chunks "
                "(file, chunk_idx, sub_idx, title, text, embedding, "
                " start_line, end_line, chunk_date, labels, state, "
                " url, duplicate_of, closing_rationale, chunk_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.file,
                    chunk.chunk_idx,
                    chunk.sub_idx,
                    chunk.title,
                    chunk.text,
                    embedding,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.chunk_date,
                    chunk.labels,
                    chunk.state,
                    chunk.url,
                    chunk.duplicate_of,
                    chunk.closing_rationale,
                    chash,
                ),
            )
    except sqlite3.IntegrityError as err:
        # A duplicate (file, chunk_idx, sub_idx) — a renderer bug or a corrupt
        # cache file with two `[N]` sections — must not abort the whole build.
        # Skip this file (its partial rows carry no hash stamp, so it retries).
        log(
            f"Duplicate chunk key in {plan.relpath} ({err}); skipping file.",
            verbose,
            level=LogLevel.ERROR,
        )
        return 0
    # Stamp the file's hash so an unchanged file is skipped next run. cur_hash
    # is None only if hashing failed (a rare read race); leave it unstamped
    # then so the file is retried rather than stored as NULL (value NOT NULL).
    if plan.cur_hash is not None:
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (plan.hash_key, plan.cur_hash),
        )
    reused = len(plan.chunks) - n_embed
    suffix = f" ({reused} reused)" if reused else ""
    log(
        f"  embedded {plan.relpath}: {n_embed}/{len(plan.chunks)} chunks{suffix}",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return len(plan.chunks)


def _flush_window(
    model: Any,
    plans: List[_FilePlan],
    window: List[Tuple[int, int, str]],
    mps_empty: Optional[Callable[[], None]],
    verbose: Verbosity,
) -> Iterator[Tuple[_FilePlan, List[Any]]]:
    """Embed one buffered window and yield `(plan, vectors)` for each file whose
    chunks all landed. `window` is `(plan_pos, chunk_idx, text)` triples into
    `plans`; the whole window goes to one `embed_multi` call, which lets
    sentence-transformers length-sort it for tight padding. A file with a chunk
    in a failed call is not yielded (no hash stamp → retried next run)."""
    if not window:
        return
    results: List[List[Any]] = [[None] * len(plan.texts) for plan in plans]
    try:
        vectors = list(model.embed_multi([text for _, _, text in window]))
    except Exception as err:  # pylint: disable=broad-except
        # The window failed (OOM, a provider error); drop the files it touched
        # so none is stamped half-embedded, and keep going.
        log(
            f"Embedding failed for a batch of {len(window)} chunks: "
            f"{type(err).__name__}: {err}",
            verbose,
            level=LogLevel.ERROR,
        )
        vectors = []
    for (pos, li, _), vec in zip(window, vectors):
        results[pos][li] = vec
    # Evict the allocator cache between windows so its high-water stays near one
    # window's footprint (no-op off MPS / on the remote backend).
    if mps_empty is not None:
        mps_empty()
    for pos, plan in enumerate(plans):
        buf = results[pos]
        if all(vec is not None for vec in buf):
            yield plan, buf


def _stream_embed(
    model: Any,
    plans: Iterator[_FilePlan],
    mps_empty: Optional[Callable[[], None]],
    verbose: Verbosity,
) -> Iterator[Tuple[_FilePlan, List[Any]]]:
    """Embed planned files in a bounded streaming pass, yielding `(plan,
    vectors)` as each file completes.

    Embedding file by file fed the model — and, on MPS, its caching allocator —
    a jarring short/tall sequence of batch shapes (a two-line issue, then a
    200-message thread), fragmenting it into a multi-GB peak. Here chunks are
    buffered across files up to `_EMBED_BUFFER`, then embedded in one call —
    giving sentence-transformers a wide length-sort window (tight padding, few
    calls) without pooling the whole corpus. At most one buffer's worth of
    chunks (plus the files feeding it) is resident, so memory is bounded by the
    buffer, not the corpus, and the caller commits per window. Whole files are
    added before the size check, so a window holds only complete files — none
    straddles two windows. Vectors are padding- and order-invariant, so the
    output matches any other batching (no re-embed)."""
    window_plans: List[_FilePlan] = []
    window: List[Tuple[int, int, str]] = []
    for plan in plans:
        pos = len(window_plans)
        window_plans.append(plan)
        window.extend((pos, li, text) for li, text in enumerate(plan.texts))
        if len(window) >= _EMBED_BUFFER:
            yield from _flush_window(model, window_plans, window, mps_empty, verbose)
            window_plans, window = [], []
    yield from _flush_window(model, window_plans, window, mps_empty, verbose)


def _file_bytes(cache_dir: str, relpath: str) -> int:
    """A planned file's byte size, or 0 if unreadable. The numerator unit for
    the byte-weighted embed progress — matches the `pending_bytes` denominator,
    which is summed the same way."""
    try:
        return os.path.getsize(os.path.join(cache_dir, relpath))
    except OSError:
        return 0


#: Don't show the embed ETA below this percent. A linear extrapolation from
#: the first few percent is dominated by startup/warm-up and reads as wildly
#: wrong; by ~a fifth of the way in enough of the run has been observed for it
#: to be worth showing.
_ETA_MIN_PCT = 20


def _fmt_secs(secs: float) -> str:
    """`45s` / `3m12s` — matches gather_status's own elapsed format so the two
    times in one status line read consistently."""
    whole = max(0, int(secs))
    return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def _embed_progress(
    done_bytes: int,
    pending_bytes: int,
    start: float,
    verbose: Verbosity,
    detail: Optional[Callable[[str], None]],
) -> None:
    """Emit a byte-weighted embed progress line — to the STATUS log and, when
    the gather passed one, to `detail` (which `gather_status` surfaces as
    `stage_detail`). Shared by the on-device and remote paths so both report
    identically.

    The `detail` phrase is self-describing (`"7% embedded, ~2m10s left"`)
    because it lands in gather_status next to a *gather*-total elapsed with no
    label — so it names its own baseline (embedding progress) and carries a
    stage ETA. The ETA is a linear extrapolation from bytes done; the embed
    rate is ~constant, so it is a reasonable estimate, marked `~` — but withheld
    below `_ETA_MIN_PCT`, where the extrapolation is still noise. Bytes track
    embed cost better than a file count and are known up front without
    pre-chunking."""
    elapsed = time.time() - start
    pct = 100 * done_bytes // pending_bytes if pending_bytes else 100
    eta = ""
    # Withhold the ETA until enough of the run has been seen (see _ETA_MIN_PCT);
    # an estimate from the first few percent is noise and misleads the reader.
    if done_bytes > 0 and _ETA_MIN_PCT <= pct < 100:
        remaining = (pending_bytes - done_bytes) * elapsed / done_bytes
        eta = f", ~{_fmt_secs(remaining)} left"
    phrase = f"{pct}% embedded{eta}"
    log(
        f"  …embedding {phrase} "
        f"({done_bytes >> 20}/{pending_bytes >> 20} MB, {elapsed:.0f}s)",
        verbose,
        level=LogLevel.STATUS,
    )
    if detail is not None:
        # Also a cancellation checkpoint on the long embed stage: the gather
        # runner polls the stop flag on each detail call.
        detail(phrase)


def build_index(
    wg: str,
    cache_dir: str,
    model_name: str = DEFAULT_EMBED_MODEL,
    rebuild: bool = False,
    verbose: Verbosity = Verbosity.STATUS,
    detail: Optional[Callable[[str], None]] = None,
) -> bool:
    """Embed all eligible files. Returns True if the index changed this run (any
    chunk embedded or pruned, or a full rebuild), False if it was already up to
    date — the caller uses this to skip the topic-map recompute (issue #190).

    Incremental: chunks for an unchanged file (same content, same model) are
    skipped. Pass rebuild=True to drop and re-embed everything.

    `detail`, if given, is called (throttled) with a short progress string like
    `"45%"` during the on-device embed — the gather passes its stage tracker's
    `detail` so the percentage surfaces through `gather_status`. It doubles as a
    cancellation checkpoint on the otherwise-opaque embed stage.
    """
    model = _get_embed_model(model_name, verbose)
    if model is None:
        return False
    # Serialise concurrent builds of the same corpus -- a second gather, or
    # a future gather-triggering MCP tool -- so they don't interleave writes
    # to one scratch DB.
    with file_lock(_db_path(wg) + ".lock"):
        # Build into a scratch copy and swap it in atomically, so a concurrent
        # reader (an MCP query mid-gather) never sees the half-populated index
        # the periodic-commit build would otherwise expose. The scratch is
        # seeded from the live index, so the build stays incremental.
        build_path = seed_build_db(wg)
        try:
            changed = _build_index_locked(
                wg, cache_dir, model, model_name, rebuild, verbose, build_path, detail
            )
        except BaseException:
            discard_build_db(wg)
            raise
        promote_build_db(wg)
        return changed


def _build_index_locked(  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    wg: str,
    cache_dir: str,
    model: Any,
    model_name: str,
    rebuild: bool,
    verbose: Verbosity,
    build_path: Optional[str] = None,
    detail: Optional[Callable[[str], None]] = None,
) -> bool:
    conn = _open_db(wg, build_path)
    cur = conn.cursor()

    # Track which model produced the existing index; rebuild if it changed.
    cur.execute("SELECT value FROM meta WHERE key='model'")
    row = cur.fetchone()
    existing_model = row[0] if row else None
    if existing_model and existing_model != model_name:
        log(
            f"Model changed ({existing_model} -> {model_name}); rebuilding index.",
            verbose,
            level=LogLevel.STATUS,
        )
        rebuild = True

    # A chunker change alters chunk boundaries but not the model id, so the
    # model check above can't catch it. Record the chunker version and
    # rebuild on mismatch, so an upgrade that changes how text is cut
    # transparently re-chunks + re-embeds each WG on its next gather. A
    # pre-versioning index (an existing `model` row but no `chunker_version`)
    # counts as a mismatch — it was built by the old char-window chunker.
    cur.execute("SELECT value FROM meta WHERE key='chunker_version'")
    row = cur.fetchone()
    existing_chunker = row[0] if row else None
    if existing_model and existing_chunker != CHUNKER_VERSION:
        log(
            f"Chunker changed ({existing_chunker or 'pre-v2'} -> "
            f"{CHUNKER_VERSION}); rebuilding index.",
            verbose,
            level=LogLevel.STATUS,
        )
        rebuild = True

    # Probe the embedding dimension up front (one embed; negligible against
    # a bulk build). Recording it is provenance beyond the model-id string;
    # comparing it catches a silent backend change that keeps the same id
    # but emits a different width -- mixing widths would corrupt the packed
    # matrix, so a dimension change forces a rebuild.
    embed_dim: Optional[int] = None
    try:
        embed_dim = len(list(model.embed("dimension probe"))) or None
    except Exception as err:  # pylint: disable=broad-except
        # If the probe fails the per-file embeds below will too; don't abort
        # here, just skip the dimension guard for this run.
        log(
            f"Could not probe embedding dimension: {type(err).__name__}: {err}",
            verbose,
            level=LogLevel.PROGRESS,
        )
    if embed_dim is not None:
        cur.execute("SELECT value FROM meta WHERE key='embed_dim'")
        row = cur.fetchone()
        existing_dim = int(row[0]) if row and row[0] else None
        if existing_model and existing_dim and existing_dim != embed_dim:
            log(
                f"Embedding dimension changed ({existing_dim} -> {embed_dim}); "
                "rebuilding index.",
                verbose,
                level=LogLevel.STATUS,
            )
            rebuild = True

    if rebuild:
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM meta")

    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('model', ?)", (model_name,)
    )
    # A rebuild clears meta (above), which would drop the schema_version the
    # read-only search path checks. The physical schema is current (_open_db
    # created / migrated it), so restamp it to keep meta consistent with the
    # table; otherwise a rebuilt index reads as an outdated schema.
    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(_SCHEMA_VERSION),),
    )
    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('chunker_version', ?)",
        (CHUNKER_VERSION,),
    )
    if embed_dim is not None:
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('embed_dim', ?)",
            (str(embed_dim),),
        )

    files = _eligible_files(cache_dir, wg)
    log(
        f"Embedding scan: {len(files)} files in cache, model={model_name}",
        verbose,
        level=LogLevel.STATUS,
    )

    cur.execute("SELECT DISTINCT file FROM chunks")
    already = {row[0] for row in cur.fetchall()}

    # Prune chunks for files no longer eligible: a draft that has since
    # become `rfc`/`repl` (now skipped by _eligible_files) or a file
    # removed from the cache. The incremental path keys on content hash, so
    # it would otherwise never re-touch these and their stale chunks would
    # linger. Doing it here migrates an existing index on the next gather
    # with no --rebuild. (No-op after a rebuild — chunks was just cleared.)
    eligible_rel = {os.path.relpath(p, cache_dir) for p in files}
    orphans = already - eligible_rel
    if orphans:
        for orphan in orphans:
            cur.execute("DELETE FROM chunks WHERE file=?", (orphan,))
        already -= orphans
        log(
            f"Pruned {len(orphans)} now-ineligible file(s) from the index.",
            verbose,
            level=LogLevel.STATUS,
        )

    # Did the vector set change this run? A rebuild or a prune already changed it;
    # any written file below sets it too. Lets the caller skip the topic-map
    # recompute when nothing changed (issue #190).
    changed = rebuild or bool(orphans)

    # Content hashes for every eligible file, computed once and reused by both
    # the pending-count pass and the embed pass below, so each file is read at
    # most once for hashing. None means the file couldn't be read; treated as
    # changed so it is retried (the chunker then skips it cleanly).
    file_hashes: Dict[str, Optional[str]] = {
        os.path.relpath(p, cache_dir): _file_hash(p) for p in files
    }

    # One-time migration: older indexes keyed incremental skips on file mtime
    # (`mtime:<relpath>` rows). The switch to content hashes makes those rows
    # dead weight, and their absence as `hash:` rows means every file re-embeds
    # once on this first post-upgrade gather (correct — same effect as a
    # rebuild, file by file). Sweep the stale rows so meta carries one scheme.
    cur.execute("DELETE FROM meta WHERE key LIKE 'mtime:%'")

    # Quick first pass: how many files actually need re-embedding?
    # The cache is incremental, so most re-gathers touch only a handful
    # of files — let the user see that up front instead of waiting
    # silently through 280 unchanged-file skips.
    pending = 0
    # Total bytes of files that need embedding — the denominator for the
    # on-device byte-weighted progress %. File bytes (a cheap stat, no read)
    # track embed cost better than a file count, and are known here without
    # pre-chunking; the on-device path advances the numerator as each file's
    # bytes are written.
    pending_bytes = 0
    for path in files:
        relpath = os.path.relpath(path, cache_dir)
        hash_key = f"hash:{relpath}"
        cur_hash = file_hashes[relpath]
        cur.execute("SELECT value FROM meta WHERE key=?", (hash_key,))
        prev = cur.fetchone()
        if relpath in already and prev and cur_hash is not None and prev[0] == cur_hash:
            continue
        pending += 1
        try:
            pending_bytes += os.path.getsize(path)
        except OSError:
            pass
    if pending == 0:
        log(
            "Embedding index already up to date.",
            verbose,
            level=LogLevel.STATUS,
        )
    else:
        log(
            f"Embedding {pending} new / changed file(s)...",
            verbose,
            level=LogLevel.STATUS,
        )

    total_new = 0
    start = time.time()
    files_done = 0
    # Chunks written since the last flush — drives the chunk-count side of the
    # write-commit cadence (see `_FLUSH_EVERY_CHUNKS`).
    chunks_since_flush = 0
    # Byte-weighted embed progress, shared by both paths: the numerator accrues
    # each written file's bytes against `pending_bytes`, emitted (STATUS log +
    # gather_status `detail`) at most every `_PROGRESS_SECS`.
    done_bytes = 0
    embed_last = start
    # On-device only: the MPS allocator evictor, to cap the reserved pool on the
    # opt-in `=mps` path (`_flush_window` and the flush cadence call it). None
    # off MPS and on the remote backend (embedding happens over HTTP).
    if is_remote_embed_model(model_name):
        mps_empty = None
    else:
        mps_empty = _mps_empty_cache()
    # Embed the planned files and write each result on this (main) thread, so
    # SQLite stays single-writer. The on-device model streams the corpus through
    # a bounded length-sort buffer (`_stream_embed`); the remote backend overlaps
    # its per-file network round-trips through a bounded pool. `workers` sizes
    # that pool and is used on the remote path only (it can be 1 — serial per
    # file — which is NOT the on-device path).
    workers = embed_concurrency() if is_remote_embed_model(model_name) else 1

    def _record(plan: _FilePlan, vectors: List[Any]) -> None:
        """Write one file's result and advance the flush cadence. Called from
        the main thread by both paths, so the counters and the cursor need no
        locking. Progress is reported by the caller (byte-weighted, via
        `_embed_progress`); this only writes and commits."""
        nonlocal total_new, files_done, chunks_since_flush, changed
        # A planned file reaching here means its content changed; mark the index
        # changed before writing, so the topic-map recompute still fires even if
        # the write returns 0 after mutating (the IntegrityError path DELETEs the
        # file's old chunks first). Conservative: the rare no-mutation skip (a
        # vector/chunk count mismatch, checked before any DELETE) also flips it,
        # which only over-recomputes — the safe direction.
        changed = True
        written = _write_file(cur, plan, vectors, verbose)
        if not written:
            return
        total_new += written
        files_done += 1
        chunks_since_flush += written
        # Periodic maintenance: commit so a crash doesn't discard the whole
        # build (we'd otherwise commit only at the end, and a WAL rollback loses
        # every embedded file), and evict the MPS allocator cache (opt-in =mps
        # path) so its high-water doesn't climb. Dual cadence: the chunk count
        # bounds a dense window, the file count is a durability floor over
        # sparse files.
        if (
            chunks_since_flush >= _FLUSH_EVERY_CHUNKS
            or files_done % _FLUSH_EVERY_FILES == 0
        ):
            conn.commit()
            gc.collect()
            if mps_empty is not None:
                mps_empty()
            chunks_since_flush = 0

    # Lazily plan (skip-check + chunk) each file on the main thread; the embed
    # call is the only off-thread work. The skip-unchanged and empty-file cases
    # fall out as a None plan.
    plans: Iterator[_FilePlan] = (
        plan
        for plan in (
            _plan_file(cur, path, cache_dir, file_hashes, already) for path in files
        )
        if plan is not None
    )

    if not is_remote_embed_model(model_name):
        # On-device: stream the corpus through a bounded length-sort buffer
        # (`_stream_embed`) — bounded memory — writing each file via `_record`
        # as it completes, reporting a byte-weighted % (`_embed_progress`).
        for plan, vectors in _stream_embed(model, plans, mps_empty, verbose):
            _record(plan, vectors)
            done_bytes += _file_bytes(cache_dir, plan.relpath)
            if time.time() - embed_last >= _PROGRESS_SECS:
                _embed_progress(done_bytes, pending_bytes, start, verbose, detail)
                embed_last = time.time()
    else:
        # Bounded fan-out: keep at most 2x workers in flight so memory stays
        # bounded (only that many files' chunks held at once), writing each
        # result as it completes. Planning and writing stay on this thread;
        # only embed_multi runs in the pool. Completion order is arbitrary —
        # the DB content is order-independent.
        max_inflight = workers * 2
        with ThreadPoolExecutor(max_workers=workers) as pool:
            inflight: Dict[Any, _FilePlan] = {}

            def _fill() -> None:
                for plan in plans:
                    inflight[pool.submit(model.embed_multi, plan.texts)] = plan
                    if len(inflight) >= max_inflight:
                        return

            _fill()
            while inflight:
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    plan = inflight.pop(fut)
                    try:
                        vectors = list(fut.result())
                    except Exception as err:  # pylint: disable=broad-except
                        log(
                            f"Embedding failed for {plan.relpath}: "
                            f"{type(err).__name__}: {err}",
                            verbose,
                            level=LogLevel.ERROR,
                        )
                        continue
                    _record(plan, vectors)
                    done_bytes += _file_bytes(cache_dir, plan.relpath)
                    if time.time() - embed_last >= _PROGRESS_SECS:
                        _embed_progress(
                            done_bytes, pending_bytes, start, verbose, detail
                        )
                        embed_last = time.time()
                _fill()

    conn.commit()
    # Finalise into a standalone single file: fold the WAL back in, then switch
    # to DELETE journal mode so the promoted DB carries no -wal/-shm sidecar.
    # That makes the atomic swap (`promote_build_db`) a single-file `os.replace`
    # a reader can never catch mid-flight, and keeps the shipped object
    # self-contained for a cloud copy or a read-only immutable replica.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    elapsed = time.time() - start
    log(
        f"Embedding done: {total_new} new chunks in {elapsed:.1f}s",
        verbose,
        level=LogLevel.STATUS,
    )
    return changed


def index_model(wg: str) -> Optional[str]:
    """Return the embedding-model id recorded in `wg`'s index, or None
    when there is no index (or no `model` row).

    Cross-corpus search uses this to decide whose scores are directly
    comparable: cosine scores from two corpora are only comparable when
    both were embedded with the same model id (see architecture.md —
    vectors are not portable across backends, and the "same" model is
    not bit-identical across runtimes). Read-only: never creates or
    migrates the DB.
    """
    if not os.path.exists(_db_path_ro(wg)):
        return None
    conn = _connect_ro(wg)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


#: Relevance/diversity tradeoff for MMR result selection. 1.0 is pure
#: relevance (the old top-k behaviour); 0.0 is pure novelty. 0.7 keeps
#: relevance dominant while breaking up near-duplicate clusters — the
#: common failure mode of plain top-k, where five chunks of one thread
#: crowd out the other threads that also matched.
_MMR_LAMBDA = 0.7
#: MMR selects its diverse k from the top-N candidates by relevance, so
#: a genuinely off-topic-but-novel chunk can't be promoted over the
#: relevant set. Also bounds the pairwise-similarity cost (an N×k matmul).
_MMR_POOL = 50


def _build_where(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    file_pattern: Optional[str],
    since: Optional[str],
    until: Optional[str],
    label: Optional[str],
    state: Optional[str],
    sort: Optional[str],
    author: Optional[str],
    role: Optional[str],
) -> Tuple[List[str], List[str]]:
    """Translate the faceted-search arguments into a list of SQL WHERE
    clauses and their bind args. Shared by `search` (query-string) and
    `related` (by-example) so both honour the same facets."""
    where_clauses: List[str] = []
    where_args: List[str] = []
    if file_pattern:
        where_clauses.append("file LIKE ?")
        where_args.append(file_pattern)
    if since:
        where_clauses.append("chunk_date IS NOT NULL AND chunk_date >= ?")
        where_args.append(since)
    if until:
        where_clauses.append("chunk_date IS NOT NULL AND chunk_date <= ?")
        where_args.append(until)
    if label:
        # Substring match on the comma-separated label column. Lowercased
        # at index time so the caller doesn't have to match case.
        where_clauses.append("labels IS NOT NULL AND labels LIKE ?")
        where_args.append(f"%{label.lower()}%")
    if state:
        # Exact match on normalised state. NULL chunks (drafts, threads,
        # transcripts) are implicitly excluded.
        where_clauses.append("state = ?")
        where_args.append(state.lower())
    if sort == "date":
        # Chronological mode excludes undated chunks — a draft windowed
        # chunk has no place in a timeline view of a debate.
        where_clauses.append("chunk_date IS NOT NULL")
    if author:
        # Substring match against chunk title — thread / issue
        # message-section titles carry the sender name. Windowed
        # draft / transcript chunks have no name in the title so
        # they implicitly drop out.
        where_clauses.append("title LIKE ?")
        where_args.append(f"%{author}%")
    if role:
        # Substring match against chunk title's role tag. Role tags
        # render as "(Chair)" / "(Chair/Author)" / "(Editor)" etc.
        # in the section header, so wrap the needle in literal
        # parens to avoid accidentally matching the role text inside
        # the body of an unrelated chunk.
        where_clauses.append("title LIKE ?")
        where_args.append(f"%({role}%")
    return where_clauses, where_args


def _open_query_db(wg: str, verbose: Verbosity) -> Optional[sqlite3.Connection]:
    """Open `wg`'s index read-only for a query, or return None with a
    logged reason (no index on disk, or a schema older than this build can
    read). Shared by `search` and `related`.

    Read-only path: the index is built and migrated by gather
    (build_index); the server never writes. `_connect_ro` avoids the
    makedirs / WAL / ALTER-TABLE migration `_open_db` performs, which is
    unnecessary for a query and unsafe against an immutable index.
    """
    if not os.path.exists(_db_path_ro(wg)):
        log(
            f"No embeddings index for {wg}. Run `ietf-llm {wg} --embed` first.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    conn = _connect_ro(wg)
    # We cannot migrate read-only, so if the on-disk schema predates this
    # version the faceted columns this query selects may be absent -- bail
    # with guidance rather than erroring on a missing column.
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key='schema_version'")
    sv_row = cur.fetchone()
    if (int(sv_row[0]) if sv_row else 1) < _SCHEMA_VERSION:
        log(
            f"Embeddings index for {wg} is an older schema; re-run "
            f"`ietf-llm {wg}` (or --rebuild-embeddings) to upgrade it.",
            verbose,
            level=LogLevel.ERROR,
        )
        conn.close()
        return None
    return conn


def _mmr_select(
    order: List[int],
    scores: "np.ndarray[Any, np.dtype[np.float32]]",
    embs: "np.ndarray[Any, np.dtype[np.float32]]",
    k: int,
    lam: float = _MMR_LAMBDA,
) -> List[int]:
    """Maximal Marginal Relevance: greedily pick k row indices from
    `order` (pre-sorted by descending relevance) that trade query
    relevance against similarity to the already-picked results, so the
    returned set covers the query rather than clustering on its single
    most-relevant facet.

    `embs` are L2-normalised, so a dot product is cosine similarity. The
    candidate set is capped at `_MMR_POOL` by relevance; if k runs past
    the pool the remainder is filled in plain relevance order.
    """
    pool = order[:_MMR_POOL]
    selected: List[int] = []
    remaining = list(pool)
    while remaining and len(selected) < k:
        if not selected:
            # Seed with the single most relevant candidate.
            selected.append(remaining.pop(0))
            continue
        sel_vecs = embs[selected]  # (s, dim)
        cand_vecs = embs[remaining]  # (c, dim)
        # Each candidate's similarity to its nearest already-picked result.
        max_sim = (cand_vecs @ sel_vecs.T).max(axis=1)  # (c,)
        rel = scores[remaining]  # (c,)
        mmr = lam * rel - (1.0 - lam) * max_sim
        selected.append(remaining.pop(int(np.argmax(mmr))))
    # k beyond the diversified pool: top up in relevance order.
    for i in order[_MMR_POOL:]:
        if len(selected) >= k:
            break
        selected.append(i)
    return selected


def _rank(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    conn: sqlite3.Connection,
    q_vec: "np.ndarray[Any, np.dtype[np.float32]]",
    *,
    k: int,
    where_clauses: List[str],
    where_args: List[str],
    sort: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    exclude: Optional["set[Tuple[str, int]]"] = None,
    diversify: bool = True,
) -> List[Hit]:
    """Score every candidate chunk against `q_vec`, collapse a long
    message's sub_idx fragments to one logical hit, select k (diversified
    by default), and build the `Hit` list.

    The query vector is the only thing that varies between callers:
    `search` embeds a query string, `related` reads an existing chunk's
    stored vector. `exclude` drops (file, chunk_idx) keys from the result
    (used to keep a `related` seed from being its own top hit).
    """
    cur = conn.cursor()
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cur.execute(
        "SELECT file, chunk_idx, title, text, embedding, "
        "start_line, end_line, labels, state, chunk_date, url, "
        "duplicate_of, closing_rationale "
        f"FROM chunks{where_sql}",
        where_args,
    )
    rows = cur.fetchall()
    if not rows:
        return []

    embs = _unpack_matrix([r[4] for r in rows])
    scores = embs @ q_vec  # cosine since both sides are normalized
    # Collapse a long message's sub_idx fragments to a single hit — its
    # best-scoring fragment — so search returns one row per logical
    # message/window (the one-hit-per-message shape the reader tools rely
    # on), with the snippet and line range taken from whichever fragment
    # actually matched. Short, unsplit chunks are their own sole fragment.
    best_by_key: Dict[Tuple[str, int], int] = {}
    for i, row in enumerate(rows):
        key = (row[0], row[1])
        if exclude and key in exclude:
            continue
        best = best_by_key.get(key)
        if best is None or scores[i] > scores[best]:
            best_by_key[key] = i
    order: List[int] = sorted(best_by_key.values(), key=lambda i: -scores[i])
    # Diversify by default (MMR), so a breadth query doesn't return the
    # same thread five times. Suppressed under sort="date": that mode is a
    # timeline, and dropping topically-adjacent messages would break the
    # early-objection → settled-position arc it exists to show.
    if diversify and sort != "date" and len(order) > k:
        top = _mmr_select(order, scores, embs, k)
    else:
        top = order[:k]
    # Chronological mode: pick top-k by relevance (so the query still
    # filters what's "about" the topic), then re-order those survivors
    # by date so the consumer reads early-objection → settled-position
    # rather than most-salient-first. Hit.score is preserved either way
    # so the caller can tell the underlying ranking apart.
    if sort == "date":
        top = sorted(top, key=lambda i: rows[i][9] or "")
    hits: List[Hit] = []
    for i in top:
        (
            file,
            chunk_idx,
            title,
            text,
            _,
            start_line,
            end_line,
            labels,
            state_val,
            _chunk_date,
            url,
            duplicate_of,
            closing_rationale,
        ) = rows[i]
        # Structure-aware snippet: prefer tables / lists when present,
        # since those carry the most ranking information per byte.
        # `snippet_chars` lets the caller override the default budget
        # (consumer feedback: defaults truncate too aggressively for
        # long-form synthesis).
        snippet = make_snippet(text, max_chars=snippet_chars)
        hits.append(
            Hit(
                score=float(scores[i]),
                file=file,
                chunk_idx=int(chunk_idx),
                title=title,
                snippet=snippet,
                start_line=int(start_line) if start_line is not None else None,
                end_line=int(end_line) if end_line is not None else None,
                labels=labels if labels else None,
                state=state_val if state_val else None,
                url=url if url else None,
                duplicate_of=(int(duplicate_of) if duplicate_of is not None else None),
                closing_rationale=closing_rationale if closing_rationale else None,
            )
        )
    return hits


def search(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    wg: str,
    query: str,
    model_name: Optional[str] = None,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    sort: Optional[str] = None,
    author: Optional[str] = None,
    role: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    diversify: bool = True,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[Hit]:
    """Return top-k chunks for a query. Returns [] if no index exists.

    Optional facets:
      - file_pattern: SQL LIKE pattern matched against the file column
        (e.g. "%-thread-%" or "%-issue-%"). % is wildcard.
      - since / until: ISO 8601 date strings; only chunks whose
        chunk_date falls in the range are considered. Chunks with
        chunk_date NULL (e.g. windowed draft chunks) are excluded when
        either bound is set, since they have no time semantics.
      - label: substring match against the (lowercased, comma-separated)
        labels column. Restricts to issue chunks tagged with that
        GitHub label — the curation work the WG already did.
      - state: 'open' or 'closed' — restricts to issue chunks with
        that resolution status. Useful for preferring the chairs'
        decision (closed issues) over older mid-debate threads, or
        vice versa.
      - sort: None (default) returns top-k by relevance.
        'date' returns the top-k by relevance then re-sorts the
        survivors chronologically (oldest first), so a consumer
        reading top-to-bottom sees how a debate evolved rather than
        what's currently most salient. NULL-dated chunks (drafts,
        transcripts, windowed) are excluded under 'date' since they
        have no place in the chronology.
      - author: substring match against the chunk title, which for
        thread / issue chunks contains the sender / commenter name
        ("Alice Chen"). Lets a consumer ask "what did Alice say
        about X" without knowing the file. Windowed draft / transcript
        chunks have no author in the title so the filter drops them.
      - role: substring match against the chunk title's role tag —
        the registry renders role-bearing messages as
        "... — Alice Chen (Chair)" / "(Chair/Author)" / "(Editor)" /
        etc. `role="Chair"` shortlists messages by people the WG
        considers procedurally responsible — high-value for "what
        did the chairs say about X" / "did anyone with formal
        responsibility weigh in" questions.
      - snippet_chars: override the default snippet budget. Useful
        when the default snippet truncates content the consumer
        wants visible inline. Applies to BOTH structured (table /
        list) and prose snippet paths.
      - diversify: when True (default), select the top-k with Maximal
        Marginal Relevance so the results cover the query rather than
        clumping on its single most-relevant facet (five chunks of one
        thread crowding out the others). Pass False for the plain
        relevance top-k. Ignored under sort="date" (a timeline must keep
        topically-adjacent messages).
    """
    conn = _open_query_db(wg, verbose)
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key='model'")
        row = cur.fetchone()
        if not row:
            return []
        indexed_model = row[0]
        if model_name and model_name != indexed_model:
            log(
                f"Query model '{model_name}' != index model '{indexed_model}'; "
                "using index model.",
                verbose,
                level=LogLevel.PROGRESS,
            )

        model = _get_embed_model(indexed_model, verbose)
        if model is None:
            return []

        try:
            q_vec = np.asarray(list(model.embed(query)), dtype=np.float32)
        except Exception as err:  # pylint: disable=broad-except
            # Same provider-variability story as build_index().
            log(
                f"Query embedding failed: {type(err).__name__}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            return []
        q_norm = float(np.linalg.norm(q_vec))
        if q_norm:
            q_vec = q_vec / q_norm

        where_clauses, where_args = _build_where(
            file_pattern, since, until, label, state, sort, author, role
        )
        return _rank(
            conn,
            q_vec,
            k=k,
            where_clauses=where_clauses,
            where_args=where_args,
            sort=sort,
            snippet_chars=snippet_chars,
            diversify=diversify,
        )
    finally:
        conn.close()


def related(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    wg: str,
    file: str,
    chunk_idx: int,
    k: int = 10,
    file_pattern: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    label: Optional[str] = None,
    state: Optional[str] = None,
    snippet_chars: Optional[int] = None,
    diversify: bool = True,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[Hit]:
    """Return the top-k chunks most similar to an existing chunk — a
    nearest-neighbour-by-example search. Returns [] if no index exists or
    the seed chunk isn't in it.

    The seed is identified by `(file, chunk_idx)` — the same identity the
    reader tools use (get_chunk_text, read_file_section). Its stored
    vector is read straight from the index (a long message's sub_idx
    fragments are averaged into one representative), so unlike `search`
    this needs no embedding backend and works even when the model can't
    load. The seed itself is excluded from the results.

    The faceted arguments (`file_pattern`, `since`/`until`, `label`,
    `state`, `snippet_chars`, `diversify`) behave exactly as in `search`.
    `file_pattern` is the lever for cross-surface bridging: seed on a
    mailing-list thread with `file_pattern="issues/%"` to find the GitHub
    issue(s) that capture the same topic, or the reverse.
    """
    conn = _open_query_db(wg, verbose)
    if conn is None:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT embedding FROM chunks WHERE file=? AND chunk_idx=?",
            (file, chunk_idx),
        )
        vecs = [r[0] for r in cur.fetchall()]
        if not vecs:
            log(
                f"No chunk {chunk_idx} in {file!r} for {wg}.",
                verbose,
                level=LogLevel.ERROR,
            )
            return []
        # A split message owns several fragment vectors; average them into
        # one representative of the whole message, then renormalise so the
        # dot product against the (normalised) corpus stays a cosine.
        seed = _unpack_matrix(vecs).mean(axis=0)
        seed_norm = float(np.linalg.norm(seed))
        if seed_norm:
            seed = seed / seed_norm

        where_clauses, where_args = _build_where(
            file_pattern, since, until, label, state, None, None, None
        )
        return _rank(
            conn,
            seed,
            k=k,
            where_clauses=where_clauses,
            where_args=where_args,
            snippet_chars=snippet_chars,
            exclude={(file, chunk_idx)},
            diversify=diversify,
        )
    finally:
        conn.close()
