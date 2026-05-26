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
from .storage import _db_path, _open_db, _pack, _unpack_matrix


@dataclass
class Hit:
    score: float
    file: str
    chunk_idx: int
    title: str
    snippet: str


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
            log(
                f"Embedding failed for {name}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            continue

        for chunk, vec in zip(chunks, vectors):
            cur.execute(
                "INSERT INTO chunks (file, chunk_idx, title, text, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                (chunk.file, chunk.chunk_idx, chunk.title, chunk.text, _pack(vec)),
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


def search(
    wg: str,
    query: str,
    model_name: Optional[str] = None,
    k: int = 10,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[Hit]:
    """Return top-k chunks for a query. Returns [] if no index exists."""
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
        log(f"Query embedding failed: {err}", verbose, level=LogLevel.ERROR)
        return []
    q_norm = float(np.linalg.norm(q_vec))
    if q_norm:
        q_vec = q_vec / q_norm

    cur.execute("SELECT file, chunk_idx, title, text, embedding FROM chunks")
    rows = cur.fetchall()
    if not rows:
        return []

    embs = _unpack_matrix([r[4] for r in rows])
    scores = embs @ q_vec  # cosine since both sides are normalized
    top = np.argsort(-scores)[:k]
    hits: List[Hit] = []
    for i in top:
        file, chunk_idx, title, text, _ = rows[i]
        snippet = text.strip().replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        hits.append(
            Hit(
                score=float(scores[i]),
                file=file,
                chunk_idx=int(chunk_idx),
                title=title,
                snippet=snippet,
            )
        )
    conn.close()
    return hits
