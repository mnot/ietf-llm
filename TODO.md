# TODO

## Add a literal-search (grep) tool (planned 2026-06-02)

The embedding-skip half of this item shipped (see below). The companion
grep tool remains: there is still **no keyword/substring search** —
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

## DONE: Don't embed concluded-draft revisions (shipped 2026-06-03)

**The single biggest embed-time win. Drafts are ~86% of chunks, and for a
mature WG the overwhelming majority are *concluded* work whose revision
stacks are historical noise a semantic index should not carry.** The
redundancy is concentrated in shipped specs: in httpbis the 787 draft-
revision files across 79 distinct drafts are dominated by RFC'd lineages
(`p2-semantics` alone = 27 revisions, `semantics` = 20, the whole
7230–7235 / 9110–9114 family at ~20–27 revisions each). In-flight drafts
are few and shallow. So the bulk of the win comes from the concluded ones.

### Policy (decided)

- **Embed in-flight (`active`) drafts in full** — *all* revisions. The
  earlier "latest-revision-only" idea is **overridden**: live-debate drafts
  are where "what changed between -02 and -03" is a real, current question,
  the volume is modest (few drafts, few revisions), and that content may not
  be anywhere else yet.
- **Skip embedding drafts whose state is `rfc` or `replaced`, by default.**
  - `rfc`: the RFC is canonical — embed it, not the draft stack. Measured:
    RFC 9110 is represented in the httpbis index by 47 draft files
    (`p2-semantics` -00..-26 + `semantics` -00..-19) PLUS `rfc9110.txt`,
    ~2,000+ chunks for one long-published doc.
  - `replaced`: catches renamed/merged lineages. `p2-semantics` never
    became an RFC *under that name* — it was replaced by `semantics`, which
    became RFC 9110. A literal "shipped as an RFC" rule would miss it and
    keep embedding that 27-revision stack, so key off **draft state**, not
    just the RFC list. (`expired`-but-never-published is a separate, low-
    volume category that sometimes holds unique content — leave it embedded
    for now; revisit if needed.)
- Estimate: removes ~600–700 of httpbis's 787 revision files while keeping
  the ~100–150 in-flight ones — i.e. ~most of the theoretical reduction,
  giving up ~10–15% to retain the useful part. Near-zero search-quality
  cost (concluded content survives in the RFC + the embedded threads/issues
  + the current draft).

### Detection cost

Not free signal today. `get_wg_documents` fetches drafts and RFCs
*separately* (`drafts.py:59`, `101`) and records only `expires` in
`documents.json` (`drafts.py:73-75`, `278-281`) — which conflates
published / replaced / merely-expired, so it's not a clean proxy. Need a
small **write-side** addition: capture each draft's Datatracker **state**
(and/or `replaced_by` / `rfc` linkage) into the manifest, then have
`_eligible_files` (or a pre-filter feeding it) consult it. Orphan rows for
now-ineligible files need a DB prune (a few lines in the build scan) or a
clear on `--rebuild-embeddings`.

Keep downloading/storing **all** revisions regardless (archive,
`read_file_section`, citing a specific `-03` all still work) — this gates
*embedding* only.

**As shipped:** `documents.json` now maps `<draft> → {expires, state}`
(loader normalises the legacy flat shape); `get_wg_documents` resolves
each draft's Datatracker state via `draft_state_slugs()`;
`_eligible_files` skips `drafts/` revisions whose base draft is `rfc` /
`repl`; and `_build_index_locked` prunes orphaned chunks so an existing
cache migrates on its next gather with no `--rebuild`. Measured against
the real httpbis cache: 651 of 787 revision files skipped, 136 in-flight
revisions + 53 RFC texts kept — matching the estimate above. This is a
**write-side** change: existing caches keep their old index until the
next `ietf-llm <wg>`.

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

