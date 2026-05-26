"""
Local semantic search over a Working Group's gathered corpus.

Chunks the cached text files, embeds them via the `llm` package (so the user
picks the embedding provider/model), and stores vectors in a per-WG sqlite
database at ~/.cache/ietf-llm/<wg>/embeddings.db.

Public surface:
  build_index(wg, model, ...)    -- (re)build the per-WG embedding index
  search(wg, query, model, k)    -- top-k chunks with metadata for a query

Chunking strategy is content-aware: mailing list and GitHub txt files are
split on their existing `===...===` record separators (one chunk per message
/ issue, which gives clean citations); drafts, RFCs, transcripts, minutes
fall back to character windows with overlap.
"""

from __future__ import annotations

import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

import numpy as np

from .utils import LogLevel, Verbosity, get_cache_dir, log

#: Default embedding model. Local, no API key, MPS-accelerated on Apple
#: Silicon via sentence-transformers. ~33M params, 384-dim. Good quality for
#: technical English with a small DB footprint and ~2-minute index time per WG.
DEFAULT_EMBED_MODEL = "sentence-transformers/BAAI/bge-small-en-v1.5"
CHUNK_SIZE = 2000  # characters
CHUNK_OVERLAP = 200
MAX_CHUNK_CHARS = 8000  # hard cap per chunk sent to the embedding model


@dataclass
class Chunk:
    file: str  # basename
    chunk_idx: int  # ordinal within the file
    title: str  # subject / issue title / section hint, for display
    text: str


@dataclass
class Hit:
    score: float
    file: str
    chunk_idx: int
    title: str
    snippet: str


# --- DB layout ---------------------------------------------------------------


def _db_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "embeddings.db")


def _open_db(wg: str) -> sqlite3.Connection:
    path = _db_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id        INTEGER PRIMARY KEY,
            file      TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            title     TEXT NOT NULL,
            text      TEXT NOT NULL,
            embedding BLOB NOT NULL,
            UNIQUE (file, chunk_idx)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return conn


def _pack(vec: Iterable[float]) -> bytes:
    arr = np.asarray(list(vec), dtype=np.float32)
    # Normalize once so search is a plain dot product
    norm = float(np.linalg.norm(arr))
    if norm:
        arr = arr / norm
    return arr.tobytes()


def _unpack_matrix(rows: List[bytes]) -> np.ndarray:
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    dim = len(rows[0]) // 4
    return np.frombuffer(b"".join(rows), dtype=np.float32).reshape(len(rows), dim)


# --- Chunking ---------------------------------------------------------------

_RECORD_SEP = re.compile(r"\n=+\n+", re.MULTILINE)
_SUBJECT_RE = re.compile(r"^Subject:\s*(.+)$", re.MULTILINE)
_FROM_RE = re.compile(r"^From:\s*(.+)$", re.MULTILINE)
_DATE_RE = re.compile(r"^Date:\s*(.+)$", re.MULTILINE)
_ISSUE_RE = re.compile(r"^Issue #(\d+):\s*(.+)$", re.MULTILINE)


def _chunk_message_file(text: str, filename: str) -> List[Chunk]:
    """Split a mailing-list-YYYY.txt file into one chunk per message."""
    parts = [p.strip() for p in _RECORD_SEP.split(text) if p.strip()]
    chunks: List[Chunk] = []
    for idx, part in enumerate(parts):
        subj_m = _SUBJECT_RE.search(part)
        from_m = _FROM_RE.search(part)
        date_m = _DATE_RE.search(part)
        title_bits = []
        if subj_m:
            title_bits.append(subj_m.group(1).strip())
        if from_m:
            title_bits.append(f"from {from_m.group(1).strip()}")
        if date_m:
            title_bits.append(date_m.group(1).strip()[:25])
        title = " · ".join(title_bits) or f"message {idx}"
        chunks.append(
            Chunk(file=filename, chunk_idx=idx, title=title, text=part[:MAX_CHUNK_CHARS])
        )
    return chunks


def _chunk_issues_file(text: str, filename: str) -> List[Chunk]:
    """Split a github-<repo>.txt file into one chunk per issue."""
    parts = [p.strip() for p in _RECORD_SEP.split(text) if p.strip()]
    chunks: List[Chunk] = []
    for idx, part in enumerate(parts):
        iss_m = _ISSUE_RE.search(part)
        if iss_m:
            title = f"#{iss_m.group(1)}: {iss_m.group(2).strip()}"
        else:
            title = f"record {idx}"
        chunks.append(
            Chunk(file=filename, chunk_idx=idx, title=title, text=part[:MAX_CHUNK_CHARS])
        )
    return chunks


def _chunk_windowed(text: str, filename: str) -> List[Chunk]:
    """Fixed-size character chunks with overlap, for drafts/RFCs/etc."""
    chunks: List[Chunk] = []
    text = text.strip()
    if not text:
        return chunks
    step = CHUNK_SIZE - CHUNK_OVERLAP
    idx = 0
    pos = 0
    while pos < len(text):
        body = text[pos : pos + CHUNK_SIZE]
        # First non-empty line as title hint
        title = next((ln.strip() for ln in body.splitlines() if ln.strip()), filename)
        if len(title) > 80:
            title = title[:77] + "..."
        chunks.append(
            Chunk(file=filename, chunk_idx=idx, title=title, text=body)
        )
        idx += 1
        pos += step
    return chunks


def _chunk_file(path: str) -> List[Chunk]:
    filename = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    lower = filename.lower()
    if "mailing-list" in lower:
        return _chunk_message_file(text, filename)
    if "-github-" in lower and lower.endswith(".txt"):
        return _chunk_issues_file(text, filename)
    return _chunk_windowed(text, filename)


def _eligible_files(cache_dir: str, wg: str) -> List[str]:
    """Files worth embedding; skip digests, JSON, binaries."""
    out = []
    for name in sorted(os.listdir(cache_dir)):
        if name.startswith(f"{wg}-_"):
            continue
        if name.endswith(".json") or name.endswith(".pdf"):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        if not (name.endswith(".txt") or name.endswith(".md")):
            continue
        out.append(path)
    return out


# --- llm wrapper ------------------------------------------------------------


_ST_PREFIX = "sentence-transformers/"


def _load_sentence_transformer(model_name: str, verbose: Verbosity) -> Any:
    """Construct (and persist registration of) a sentence-transformers model.

    `llm-sentence-transformers` expects HF model names to be added to its
    config file via `llm sentence-transformers register <name>` before they
    become addressable through `llm.get_embedding_model()`. We do that
    write-through automatically and return a directly-constructed model
    instance (the plugin's `register_embedding_models` hook only runs at
    llm startup, so we can't make it visible to `get_embedding_model` in
    the current process).
    """
    bare = model_name[len(_ST_PREFIX) :]
    try:
        # pylint: disable=import-outside-toplevel,import-error
        from llm_sentence_transformers import (  # type: ignore[import-untyped]
            SentenceTransformerModel,
            read_models,
            write_models,
        )
    except ImportError:
        log(
            "Sentence-transformers embeddings require the "
            "`llm-sentence-transformers` plugin. Install with: "
            "pipx inject ietf-llm llm-sentence-transformers",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    try:
        models = read_models()
        if not any(m.get("name") == bare for m in models):
            log(
                f"Registering '{bare}' with llm (first-time setup)...",
                verbose,
                level=LogLevel.STATUS,
            )
            models.append({"name": bare, "trust_remote_code": False})
            write_models(models)
        # Constructing the model triggers the HF download on first use.
        return SentenceTransformerModel(
            f"{_ST_PREFIX}{bare}", bare, False
        )
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"Could not load sentence-transformers model '{bare}': {err}. "
            f"Try manually: llm sentence-transformers register {bare}",
            verbose,
            level=LogLevel.ERROR,
        )
        return None


def _get_embed_model(model_name: str, verbose: Verbosity) -> Any:
    # Local sentence-transformers path: construct directly, skip llm's
    # registry (see _load_sentence_transformer docstring).
    if model_name.startswith(_ST_PREFIX):
        return _load_sentence_transformer(model_name, verbose)

    try:
        import llm  # pylint: disable=import-outside-toplevel,import-error
    except ImportError:
        log(
            "Embedding requires the `llm` package. Install with: "
            "pipx install 'ietf-llm[search]'",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    try:
        return llm.get_embedding_model(model_name)  # type: ignore[no-untyped-call]
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"Could not load embedding model '{model_name}': {err}",
            verbose,
            level=LogLevel.ERROR,
        )
        return None


# --- Public API -------------------------------------------------------------


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

    # Find which files are already indexed (cheap mtime tracking: chunks exist
    # for the basename). For accuracy we re-embed if the file is newer than
    # the meta timestamp recorded for it.
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


def get_chunk(wg: str, file: str, chunk_idx: int) -> Optional[Tuple[str, str]]:
    """Fetch the full text of a stored chunk. Returns (title, text)."""
    if not os.path.exists(_db_path(wg)):
        return None
    conn = sqlite3.connect(_db_path(wg))
    try:
        cur = conn.execute(
            "SELECT title, text FROM chunks WHERE file=? AND chunk_idx=?",
            (file, chunk_idx),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (str(row[0]), str(row[1]))
    finally:
        conn.close()


# Avoid pylint complaints in code paths that read but don't use struct.
_ = struct
