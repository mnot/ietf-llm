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
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..utils import LogLevel, Verbosity, log
from .chunking import CHUNKER_VERSION, _chunk_file, _eligible_files
from .models import DEFAULT_EMBED_MODEL, _get_embed_model
from .snippet import make_snippet
from .storage import (
    _SCHEMA_VERSION,
    _connect_ro,
    _db_path,
    _open_db,
    _pack,
    _unpack_matrix,
)

#: After every N files processed, emit a one-line STATUS progress update.
_PROGRESS_EVERY = 25
#: …or after this many seconds of silence, whichever comes first. Picked
#: short enough that a slow embed call doesn't look like the gather
#: has hung, long enough that small WGs don't get spammed.
_PROGRESS_SECS = 20.0


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

    files = _eligible_files(cache_dir, wg)
    log(
        f"Embedding scan: {len(files)} files in cache, model={model_name}",
        verbose,
        level=LogLevel.STATUS,
    )

    cur.execute("SELECT DISTINCT file FROM chunks")
    already = {row[0] for row in cur.fetchall()}

    # Quick first pass: how many files actually need re-embedding?
    # The cache is incremental, so most re-gathers touch only a handful
    # of files — let the user see that up front instead of waiting
    # silently through 280 unchanged-file skips.
    pending = 0
    for path in files:
        relpath = os.path.relpath(path, cache_dir)
        mtime_key = f"mtime:{relpath}"
        file_mtime = os.path.getmtime(path)
        cur.execute("SELECT value FROM meta WHERE key=?", (mtime_key,))
        prev = cur.fetchone()
        if relpath in already and prev and float(prev[0]) >= file_mtime:
            continue
        pending += 1
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
    # Periodic progress: emit a one-line update at STATUS level every
    # `_PROGRESS_EVERY` processed files OR every `_PROGRESS_SECS`,
    # whichever comes first. Keeps the user informed during long
    # embeds without spamming on small ones.
    files_done = 0
    last_status = start
    for path in files:
        # Relative path within the WG cache is what we store as
        # chunks.file, what consumers pass to get_chunk_text /
        # read_file_section, and what mtime tracking keys on.
        relpath = os.path.relpath(path, cache_dir)
        mtime_key = f"mtime:{relpath}"
        file_mtime = os.path.getmtime(path)
        cur.execute("SELECT value FROM meta WHERE key=?", (mtime_key,))
        prev = cur.fetchone()
        if relpath in already and prev and float(prev[0]) >= file_mtime:
            continue  # unchanged

        chunks = _chunk_file(path, relpath)
        if not chunks:
            continue

        # If we had stale chunks for this file, drop them first.
        cur.execute("DELETE FROM chunks WHERE file=?", (relpath,))

        # Embed in batches; llm models support embed_multi. A split
        # section's sub_idx 0 stores the full message in `text` but sets
        # `embed_text` to just its first window, so we embed the window —
        # the tail is covered by the later sub_idx fragments' own vectors.
        texts = [c.embed_text if c.embed_text is not None else c.text for c in chunks]
        try:
            vectors = list(model.embed_multi(texts))
        except Exception as err:  # pylint: disable=broad-except
            # Embedding failures vary by provider (HTTP errors, OOM,
            # rate limits, …) and don't share a typed hierarchy.
            log(
                f"Embedding failed for {relpath}: {type(err).__name__}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            continue

        for chunk, vec in zip(chunks, vectors):
            cur.execute(
                "INSERT INTO chunks "
                "(file, chunk_idx, sub_idx, title, text, embedding, "
                " start_line, end_line, chunk_date, labels, state, "
                " url, duplicate_of, closing_rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk.file,
                    chunk.chunk_idx,
                    chunk.sub_idx,
                    chunk.title,
                    chunk.text,
                    _pack(vec),
                    chunk.start_line,
                    chunk.end_line,
                    chunk.chunk_date,
                    chunk.labels,
                    chunk.state,
                    chunk.url,
                    chunk.duplicate_of,
                    chunk.closing_rationale,
                ),
            )
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (mtime_key, str(file_mtime)),
        )
        total_new += len(chunks)
        files_done += 1
        log(
            f"  embedded {relpath}: {len(chunks)} chunks",
            verbose,
            level=LogLevel.PROGRESS,
        )
        # Light-touch STATUS pulse so the user sees progress on long
        # embeds without --verbose. Only fires when we've actually done
        # work (the skip-unchanged branch above continues without
        # incrementing files_done).
        now = time.time()
        if files_done % _PROGRESS_EVERY == 0 or (now - last_status) >= _PROGRESS_SECS:
            elapsed = now - start
            log(
                f"  …{files_done}/{pending} files, "
                f"{total_new} chunks, {elapsed:.0f}s elapsed",
                verbose,
                level=LogLevel.STATUS,
            )
            last_status = now

    conn.commit()
    conn.close()
    elapsed = time.time() - start
    log(
        f"Embedding done: {total_new} new chunks in {elapsed:.1f}s",
        verbose,
        level=LogLevel.STATUS,
    )
    return total_new


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
    """
    if not os.path.exists(_db_path(wg)):
        log(
            f"No embeddings index for {wg}. Run `ietf-llm {wg} --embed` first.",
            verbose,
            level=LogLevel.ERROR,
        )
        return []

    # Read-only path: the index is built and migrated by gather
    # (build_index); the server never writes. _connect_ro avoids the
    # makedirs / WAL / ALTER-TABLE migration _open_db performs, which is
    # unnecessary for a query and unsafe against an immutable index.
    conn = _connect_ro(wg)
    cur = conn.cursor()
    # We cannot migrate read-only, so if the on-disk schema predates this
    # version the faceted columns this query selects may be absent -- bail
    # with guidance rather than erroring on a missing column.
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
        return []
    cur.execute("SELECT value FROM meta WHERE key='model'")
    row = cur.fetchone()
    if not row:
        conn.close()
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
        best = best_by_key.get(key)
        if best is None or scores[i] > scores[best]:
            best_by_key[key] = i
    top: List[int] = sorted(best_by_key.values(), key=lambda i: -scores[i])[:k]
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
    conn.close()
    return hits
