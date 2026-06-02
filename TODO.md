# TODO

## Embedding-time efficiency (investigated 2026-06-02)

**Verdict: don't build a shared content-addressed embedding cache. The data
doesn't justify it.** The existing mtime file-skip already captures the bulk of
available savings. Notes kept here so we don't re-investigate from scratch.

### How dedup works today

- Embedding is per-chunk (one chunk per message/comment for threads/issues;
  windowed for drafts/RFCs), stored in a **per-corpus** SQLite DB
  (`~/.cache/ietf-llm/<wg>/embeddings.db`).
- Incremental, but at **file granularity**, keyed on mtime
  (`ietf_llm/embeddings/search.py:130`). Unchanged file → skipped. Changed file
  → `DELETE FROM chunks WHERE file=?` then **re-embed every chunk in it**
  (`search.py:171-176`).
- `write_if_changed` (`ietf_llm/utils.py:182`) keeps byte-identical re-renders
  from bumping mtime, so a no-op re-gather embeds nothing.
- No content-addressing (keyed on `(file, mtime)`, never on text). Stores are
  strictly per-corpus — nothing shared.

### The two concerns we examined, measured against the real caches

Measured across the 5 gathered corpora (aipref, httpbis, tls, webbotauth,
x-agent2agent), 89,611 chunk-embeddings total.

1. **Thread/issue updates re-embed the whole file.** Real, but small. Embedding
   volume is dominated by drafts/RFCs, which are skip-if-exists (embedded once,
   never re-touched — e.g. httpbis: 31,281 of 36,481 chunks are drafts). The
   churning surface (threads/issues) is ~4–11% of chunks in the draft-heavy
   corpora, and the mtime skip means only *changed* threads re-embed on a
   re-gather. Cost concentrates in a few large *active* threads (notably one
   326-message tls thread; median thread is 3 messages and dead). Worst case —
   re-embedding all of tls's thread surface (~3,724 chunks) — is single-digit
   seconds on bge-small/MPS (estimate, not benchmarked); a real re-gather
   touches far less.

2. **Same resource cited in multiple corpora → embedded once per corpus.**
   Negligible in practice: only **0.9%** (772 of 89,611) of chunk texts are
   shared across ≥2 corpora. Corpora gather different drafts/RFCs, so there's
   almost nothing to share. Biggest pair httpbis↔tls = 526 shared texts (mostly
   RFC reference text + a few cross-posted threads).

### Other repeated-embedding cases (for completeness)

- Model switch → full wipe + rebuild (`search.py:93-103`); switching back
  re-embeds from scratch.
- Render churn → any byte change (re-resolved GitHub username, reformatted date,
  quote-elision boundary shift) bumps mtime and re-embeds the whole file even
  though most chunks are textually identical.
- Intra-corpus duplicate text (RFC text also quoted in a thread) embedded once
  per file.
- Windowed drafts are safe on re-gather (skip-if-exists; mtime stable).

### Why not the shared content-addressed cache

A global `(sha256(model_id + text) -> vector)` store would fix all of the above
in one place, but: it buys ~1% (cross-corpus) plus a few seconds of thread
re-embedding, in exchange for a shared mutable store, cross-corpus lock
contention (today unrelated gathers never contend), and a poisoning surface that
survives `--rebuild-embeddings`. Bad trade.

### If thread re-embedding ever becomes a felt pain

(Most likely in an active WG like tls during a meeting week, where a large
thread grows daily and re-embeds in full each gather.) The proportionate fix is
**per-chunk hashing inside the existing per-WG `embeddings.db`**: hash each
message-chunk, reuse unchanged ones, embed only new/edited sections. Stays
per-WG — no new isolation or poisoning surface. This is a "wait until it hurts"
optimization, not called for by current data.
