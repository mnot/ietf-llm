"""Build the per-WG embedding index, and query it.

`build_index(wg, cache_dir, ...)` walks the cache, chunks each eligible
file, embeds the chunks, and stores them in the WG's sqlite DB. The
operation is incremental: a file whose mtime hasn't advanced since the
last indexed timestamp is skipped.

`search(wg, query, ...)` reads back every stored embedding, computes
cosine similarity against a freshly-embedded query (single numpy
matmul; vectors were stored normalised), and returns the top-k hits.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from ..utils import LogLevel, Verbosity, log
from .chunking import _chunk_file, _eligible_files
from .models import DEFAULT_EMBED_MODEL, _get_embed_model
from .snippet import make_snippet
from .storage import _db_path, _open_db, _pack, _unpack_matrix


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


def build_index(
    wg: str,
    cache_dir: str,
    model_name: str = DEFAULT_EMBED_MODEL,
    rebuild: bool = False,
    verbose: Verbosity = Verbosity.STATUS,
) -> int:
    """Embed all eligible files. Returns number of chunks indexed.

    Incremental: chunks for an unchanged file (same content, same model) are
    skipped. Pass rebuild=True to drop and re-embed everything.
    """
    model = _get_embed_model(model_name, verbose)
    if model is None:
        return 0

    conn = _open_db(wg)
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

    if rebuild:
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM meta")

    cur.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('model', ?)", (model_name,)
    )

    files = _eligible_files(cache_dir, wg)
    log(
        f"Embedding scan: {len(files)} files in cache, model={model_name}",
        verbose,
        level=LogLevel.STATUS,
    )

    cur.execute("SELECT DISTINCT file FROM chunks")
    already = {row[0] for row in cur.fetchall()}

    total_new = 0
    start = time.time()
    for path in files:
        name = os.path.basename(path)
        mtime_key = f"mtime:{name}"
        file_mtime = os.path.getmtime(path)
        cur.execute("SELECT value FROM meta WHERE key=?", (mtime_key,))
        prev = cur.fetchone()
        if name in already and prev and float(prev[0]) >= file_mtime:
            continue  # unchanged

        chunks = _chunk_file(path)
        if not chunks:
            continue

        # If we had stale chunks for this file, drop them first.
        cur.execute("DELETE FROM chunks WHERE file=?", (name,))

        # Embed in batches; llm models support embed_multi
        texts = [c.text for c in chunks]
        try:
            vectors = list(model.embed_multi(texts))
        except Exception as err:  # pylint: disable=broad-except
            # Embedding failures vary by provider (HTTP errors, OOM,
            # rate limits, …) and don't share a typed hierarchy.
            log(
                f"Embedding failed for {name}: {type(err).__name__}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            continue

        for chunk, vec in zip(chunks, vectors):
            cur.execute(
                "INSERT INTO chunks "
                "(file, chunk_idx, title, text, embedding, "
                " start_line, end_line, chunk_date, labels, state, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.file,
                    chunk.chunk_idx,
                    chunk.title,
                    chunk.text,
                    _pack(vec),
                    chunk.start_line,
                    chunk.end_line,
                    chunk.chunk_date,
                    chunk.labels,
                    chunk.state,
                    chunk.url,
                ),
            )
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (mtime_key, str(file_mtime)),
        )
        total_new += len(chunks)
        log(
            f"  embedded {name}: {len(chunks)} chunks",
            verbose,
            level=LogLevel.PROGRESS,
        )

    conn.commit()
    conn.close()
    elapsed = time.time() - start
    log(
        f"Embedding done: {total_new} new chunks in {elapsed:.1f}s",
        verbose,
        level=LogLevel.STATUS,
    )
    return total_new


def search(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
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
    """
    if not os.path.exists(_db_path(wg)):
        log(
            f"No embeddings index for {wg}. Run `ietf-llm {wg} --embed` first.",
            verbose,
            level=LogLevel.ERROR,
        )
        return []

    conn = _open_db(wg)
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
    use_model = indexed_model

    model = _get_embed_model(use_model, verbose)
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
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    cur.execute(
        "SELECT file, chunk_idx, title, text, embedding, "
        "start_line, end_line, labels, state, chunk_date, url "
        f"FROM chunks{where_sql}",
        where_args,
    )
    rows = cur.fetchall()
    if not rows:
        return []

    embs = _unpack_matrix([r[4] for r in rows])
    scores = embs @ q_vec  # cosine since both sides are normalized
    top: List[int] = [int(i) for i in np.argsort(-scores)[:k]]
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
            file, chunk_idx, title, text, _,
            start_line, end_line, labels, state_val, _chunk_date, url,
        ) = rows[i]
        # Structure-aware snippet: prefer tables / lists when present,
        # since those carry the most ranking information per byte.
        snippet = make_snippet(text)
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
            )
        )
    conn.close()
    return hits
