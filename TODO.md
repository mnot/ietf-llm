# TODO

## Add a literal-search (grep) tool (planned 2026-06-02)

The embedding-skip half of this item shipped (concluded-draft revisions
are no longer embedded). The companion grep tool remains: there is still
**no keyword/substring search** —
semantic `search_corpus` (the embedding index) is the only
content-discovery path, so "not embedded" == "not blind-discoverable."
That's the one real gap from skipping concluded revisions: wording that
lived only in a superseded revision and was removed before the RFC can't
be found cold (it can still be *read* once something points at the
revision, and `list_files` shows it on disk tagged "not indexed").

Add a substring/grep tool over the on-disk cache (esp. `drafts/`). It
needs no index, restores literal discoverability of every revision, and
is a *better* fit for historical-draft questions, which tend to be
literal ("which revision first mentioned this parameter / section /
term") rather than semantic.

## Embedding-time efficiency (investigated 2026-06-02)

**Verdict: don't build a shared content-addressed embedding cache. The data
doesn't justify it.** The existing per-file content-hash skip already captures
the bulk of available savings. Notes kept here so we don't re-investigate from
scratch.

### How dedup works today

- Embedding is per-chunk (one chunk per message/comment for threads/issues;
  windowed for drafts/RFCs), stored in a **per-corpus** SQLite DB
  (`~/.cache/ietf-llm/<wg>/embeddings.db`).
- Incremental, but at **file granularity**, keyed on each file's content hash
  (`ietf_llm/embeddings/search.py:147`). Unchanged bytes → skipped. Changed file
  → `DELETE FROM chunks WHERE file=?` then **re-embed every chunk in it**.
- Keying on content (not mtime) means a byte-identical re-render embeds nothing
  even on a fresh replica that materialised the file with a new mtime — the hash
  matches, so it is skipped. (`write_if_changed`, `ietf_llm/utils.py`, still
  avoids the file-I/O churn, but the embed skip no longer depends on it.)
- Content-addressing is **per-file only** — a file's hash gates the whole file,
  never per-chunk or across corpora. Stores are strictly per-corpus.

### The two concerns we examined, measured against the real caches

Measured across the 5 gathered corpora (aipref, httpbis, tls, webbotauth,
x-agent2agent), 89,611 chunk-embeddings total.

1. **Thread/issue updates re-embed the whole file.** Real, but small. Embedding
   volume is dominated by drafts/RFCs, which are skip-if-exists (embedded once,
   never re-touched — e.g. httpbis: 31,281 of 36,481 chunks are drafts). The
   churning surface (threads/issues) is ~4–11% of chunks in the draft-heavy
   corpora, and the content-hash skip means only *changed* threads re-embed on a
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
  quote-elision boundary shift) changes the file hash and re-embeds the whole
  file even though most chunks are textually identical.
- Intra-corpus duplicate text (RFC text also quoted in a thread) embedded once
  per file.
- Windowed drafts are safe on re-gather (skip-if-exists; bytes stable).

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

## Reconcile mailing-list identities via Datatracker person id (planned 2026-06-02)

Use Datatracker's `email -> person` mapping as the identity **spine** so the
same participant is recognised across addresses and over time — *continuity of
participation*, not per-person profiling. Complements the GitHub-author linking
(PR #27, `gather/datatracker_github.py`): that attaches GitHub logins to a
Person; this consolidates the mail side itself.

### Why

Mail-side dedup today (`people.py`) merges two addresses only when they share a
display name or one is a DMARC rewrite. It misses the common long-timer case:
one human posting under unrelated addresses with *different* name spellings
(`M. Nottingham <mnot@fastly.com>` vs `Mark Nottingham <mnot@mnot.net>`).
Datatracker records all of a person's addresses — active and historical — under
one person id, closing exactly that gap, and at higher precision than
name-string equality (curated records, not a fuzzy match).

### Approach (write-side)

- New `gather/datatracker_people.py`: `resolve_addresses(addresses) ->
  {address: person_uri}` via the email endpoint filtered with `address__in`,
  chunked (~50/request). Verified against the live API: both `address__in` and
  `person__in` batch-filter, and each row already carries its `person` uri — so
  grouping needs **no** per-person follow-up request (a handful of calls per WG,
  not one per participant). Global, WG-independent cache like
  `_datatracker-github.json`.
- In `build_registry`, right after `_ingest_mail` and **before** the GitHub
  passes: collect distinct normalised mail addresses already in the registry,
  resolve, group by person uri, and `_merge_persons` (helper added in PR #27)
  any group spanning >=2 registry Persons. Person uri is the join key. Running
  first means the GitHub passes inherit the consolidated identities for free.

### Decisions

- **Canonical name: merge-only.** Do NOT wholesale-adopt Datatracker's `name`;
  only upgrade when the current canonical is still email-ish (matches the
  existing upgrade rule). Avoids churning display names people already
  recognise. Flip to "Datatracker name wins" only if we decide it should be
  authoritative.
- **Precision guards.** Relay / `noreply` addresses never reach the resolver
  (`_normalise_email` drops them). Watch role / alias addresses (`chairs@`,
  draft aliases) — a mis-merge corrupts identities corpus-wide, so keep the same
  conservative posture as the GitHub linker.
- **Write-side:** existing caches keep their old identities until the next
  `ietf-llm <wg>` gather.

### Unknown

Yield per WG (how many real continuity-merges) is unmeasured. Guess: a handful,
not dozens — most participants use one address and the name-merge already
catches the rest. Report before/after on httpbis when built, same as PR #27.

## ETag http-cache is host-local; degrades with fleet size (noted 2026-06-06)

The Datatracker conditional-GET store (`gather/datatracker.py` `_HttpCache`,
one file at `~/.cache/ietf-llm/.http-cache.json`) is plain gather machinery: it
is written directly to `get_cache_dir()` and **never routed through the
`CorpusStore` / publish seam**, so it is per-host. A bounded-eviction policy
(last-used age window + LRU entry cap, applied at flush) now keeps each host's
file from growing without bound — see `_HttpCache._evict`.

The open item is cross-host *effectiveness*, not size. Gathers have **no host
affinity**: any host's `start_gather` runs the gather on that host, and the
per-corpus lease (`KvControlPlane.acquire_lease`, `kv_control.py`) only
serialises concurrent writes — it does not pin or route a corpus to a "home"
host. So a corpus
gathered on host A warms only A's ETag store; if the next gather of that corpus
lands on host B, B is cold and re-downloads full bodies (no 304s). Hit rate is
therefore whatever fraction of repeat gathers happen to land on the same host,
and it falls as the fleet grows. Fine for single-host / dev; a tax on bandwidth
in a symmetric multi-host fleet.

### Options if cross-host ETag reuse ever matters

- **Route the cache through `CorpusStore`** (the clean fit): small ETag /
  last-used keys on the control plane (the `KvControlPlane` / `KvStore` seam
  already there for the version pointer and lease), bodies on the blob plane.
  Gets cross-host
  reuse *and* the same atomicity/lease story the corpus store has, instead of a
  shared mutable file.
- **Do NOT** just point `IETF_LLM_CACHE_DIR` at a shared mount. That technically
  shares the file but neither the existing `flush()` nor the new eviction is
  concurrency-safe across hosts: the atomic `os.replace` prevents corruption but
  is last-writer-wins, so concurrent flushes lose each other's entries (→ extra
  re-downloads; never incorrect, since revalidation still gates on the live
  response). The touch-on-`get` added with eviction widens that lost-update
  window slightly.

Not warranted by current usage — bandwidth from cold re-gathers is modest and
the eviction bound already removes the unbounded-growth risk. Revisit only if a
multi-host deployment shows the re-download cost is real.

## Server-side CopyObject for unchanged blobs on re-publish (noted 2026-06-08)

**Deferred.** `CloudCorpusStore.publish` (`corpus_store_cloud.py`) re-uploads
*every* file from the workspace into a fresh `corpora/<name>/versions/<version>/`
prefix on each gather — full bytes, no diff against the prior version, no
server-side copy (`os.walk` + `self._blobs.put` per file). For a large corpus
re-gathered often, that upload bandwidth could become a real cost.

The proportionate fix is **`CopyObject` for byte-identical files**, not full
content-addressed dedup. It targets exactly the upload-bandwidth concern while
preserving the property that makes the storage model simple.

### Why CopyObject fits here (better than CAS)

A server-side copy produces an **independent object** at the destination key.
So version N's blob is its own object — deleting version N-1's prefix doesn't
touch it. The load-bearing invariant survives: each `versions/<version>/` prefix
stays fully self-contained, and `_reap_versions` remains a dumb `delete_prefix`
with **zero refcounting/GC**. CAS dedup would have forced a refcount story; this
doesn't. Other things already line up:

- **ETag == MD5.** Everything is single-part `put_object` (no multipart
  anywhere), so content comparison is cheap and reliable.
- **Single writer.** Publish holds the per-corpus lease — source version stable
  for the duration, no concurrent pointer flip.
- **Source exists at copy time.** Reap runs *after* the pointer flip and keeps
  current + previous, so the immediately-prior version is present to copy from.

### Caveats (in priority order)

1. **Saves upload bytes, not storage.** Each version still stores its own full
   copy (CopyObject duplicates server-side); with `retain_versions=2` still ~2x
   at rest. Right tool for bandwidth, no help for storage — only CAS gives that.
2. **Hit rate depends entirely on writer determinism.** CopyObject only fires
   for byte-identical files. If gather output embeds a wall-clock timestamp or
   non-deterministic ordering, an "unchanged" thread differs byte-wise and won't
   dedup. **Verify this first** — gather a corpus twice and diff two version
   prefixes. If they're mostly identical, proceed; else fix determinism first.
3. **`embeddings.db` — the biggest blob — almost never benefits.** Re-gather
   rebuilds the index, so the largest single file changes every time. The win
   concentrates on the static long tail (old threads, RFCs, unchanged drafts).
4. **Implement via manifest hashes, not per-file HEAD.** Add a per-file `hash`
   to the manifest (`corpus_store_cloud.py` ~line 500) going forward; publish
   then = 1 GET of prev manifest + compare local hashes -> CopyObject matches,
   PUT the rest. Additive: old manifests lack hashes -> graceful full-upload
   fallback. (HEAD-per-blob for ETag is HEAD+COPY = 2 requests vs 1 PUT per
   unchanged file — a net loss for tiny files; avoid.) Note the break-even: for
   many *tiny* files a COPY and a PUT are both one request and the tiny bytes are
   negligible, so the win is real only for medium/large files / large volume.
5. **Seam + provider.** Add `copy(src_key, dst_key)` to `BlobStore`
   (`corpus_blobs.py`): `copy_object(CopySource=...)` for `S3BlobStore`,
   `shutil.copyfile`/hardlink for `FileBlobStore` — keeps the abstraction clean
   instead of reaching into boto3 from publish. CopyObject is core S3 API (R2,
   MinIO support same-bucket copy). Gotcha: AWS caps a single `CopyObject` at
   5 GB (multipart copy above that); confirm R2's cap. Cheap guard: fall back to
   PUT above a size threshold (also covers `embeddings.db` if it grows huge).
6. **Always fall back to PUT, best-effort.** Missing source, copy error, first
   gather, reaped source -> just `put`. A copy failure must never fail an
   otherwise-successful publish (mirror the `_reap_versions` best-effort stance).

### Gate before building

Confirm gather output is byte-deterministic for unchanged content (caveat 2) —
that decides whether this pays off at all. If it is: manifest gains per-file
hashes, `BlobStore.copy` added, publish copies hash-matches from the previous
version and PUTs the rest, with size-threshold and error fallbacks to PUT.

