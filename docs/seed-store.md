# Seed store (design)

**Status:** implemented — issue [#182](https://github.com/mnot/ietf-llm/issues/182).
The producer (`scripts/publish_seeds.py`), consumer (`ietf_llm/seed/`,
`IETF_LLM_SEED_URL`), and the gather seed step are in place. The one thing still
pending before opt-out is live for everyone: a **default hosting URL**
(`config.service._DEFAULT_SEED_URL` is `None`, so seeding is off until an operator
sets `IETF_LLM_SEED_URL`). Back to the [docs index](README.md).

## The problem

Embedding a corpus is the expensive part of a gather — CPU time to run the local
model, and upstream load on Datatracker / IMAP / GitHub to fetch the source. All
of that produces **public** data: the same `httpbis` corpus embedded on one
laptop is byte-for-byte useful on any other. Today every user pays the full cost
independently. `embeddings.db` files range 2–116 MB, so re-deriving them per
client is real, avoidable work.

The idea: publish a small set of curated corpora — "interesting" groups, a ~1y
window each, refreshed ~monthly — to a **static, publicly-hosted directory** (the
*seed store*), and let a client **seed** a corpus from it as the basis of its own
work, then grow it by freshening. If a group is covered, a client only gathers and
embeds what has changed since the snapshot (generally < 1 month of material)
instead of starting cold.

**Scope: this is for local-focused users only.** It is not the cloud
`CorpusStore` backend (that solves a different problem — a replicated serving
fleet with a control plane). This is a laptop running the default `local` backend
that wants to bootstrap a corpus from a public mirror. No control plane, no
leases, no compare-and-swap — just static files a client downloads.

## What is published: the matched pair

A published corpus is the **`files/` tree and its `embeddings.db` together**, not
the index alone. This is the load-bearing decision; the rationale:

- `embeddings.db`'s `meta` table records a per-file content SHA-256
  (`hash:<relpath>`). A re-gather skips embedding any file whose current bytes
  hash-match the stored value (`_plan_file` in `embeddings/search.py`). Shipping
  the *exact* `files/` the vectors were computed against guarantees every file
  unchanged in the window hash-matches and is skipped **completely** — so the
  consumer re-embeds only material that genuinely postdates the snapshot.
- The embed-skip is **per-file, not per-chunk** (see
  [#183](https://github.com/mnot/ietf-llm/issues/183)). A thread that differs
  from the producer's rendering *at all* re-embeds wholesale. Thread/issue
  rendering depends on `people.Registry` (affiliations, GitHub-login linking —
  both pulled live from Datatracker and time-varying), so *independent* gathers
  do **not** reliably reproduce byte-identical `files/`. Shipping the producer's
  `files/` is what makes the skip deterministic rather than a coin-flip on the
  bulk of the corpus.
- The consumer needs `files/` anyway: `overview`, `read_digest`,
  `read_file_section`, `read_topic`, and `get_by_url` read it directly, the
  digests are not in the DB, and corpus existence is a `files/` check. An
  embeddings-only ship would not be a usable corpus.
- Seeding `files/` also cuts **source burden**: the gather's existence-checks
  then skip re-downloading immutable inputs (drafts, RFCs, minutes), so the
  seed store saves both embedding compute *and* upstream fetches.

The `files/` text is largely redundant with the chunk text already stored in the
DB (`chunks.text`), so shipping it adds roughly 30–60 % over the index alone,
while the vectors dominate the size either way — a good trade for a deterministic,
complete skip.

**Excluded from the bundle:** `raw/` (year mail-dumps + raw GitHub — not indexed,
NotebookLM/grep only), `imap-cache/` (lives outside `<corpus>/`, large, and its
content is already materialised in `threads/`), and `gather-metrics.json` (the
producer's egress accounting). A consumer that later wants the NotebookLM export
re-gathers to regenerate `raw/`. **Included:** `files/` (minus `raw/`),
`embeddings.db`, `topics.json`, and the incremental-gather manifests
(`documents.json`, `materials.json`, `last-gathered`, `github/`) so the follow-on
gather is fully incremental.

## The format: a JSON-described static directory

A directory servable by any static host (a web server, an R2/S3 public bucket,
GitHub Pages). The client is pointed at its base URL and needs only HTTPS GET.

**Root `index.json`** — the entry point and the compatibility gate:

```json
{
  "format": 1,
  "generated": "2026-07-01T00:00:00Z",
  "schema_version": 8,
  "embedding_model": "sentence-transformers/BAAI/bge-small-en-v1.5",
  "chunker_version": 7,
  "vector_dim": 384,
  "corpora": [
    { "name": "httpbis", "kind": "group", "subject": "HTTP Working Group",
      "window_months": 12, "gathered": "2026-07-01T00:00:00Z",
      "version": "20260701T000000Z", "manifest": "httpbis/manifest.json",
      "bytes": 47185920 }
  ]
}
```

**Per-corpus `<name>/manifest.json`** — self-describing (repeats the
compatibility tuple so a corpus fetched directly is still checkable) and points at
the payload with its integrity hash:

```json
{ "name": "httpbis", "version": "20260701T000000Z",
  "schema_version": 8, "embedding_model": "…", "chunker_version": 7,
  "vector_dim": 384, "window_months": 12, "gathered": "2026-07-01T00:00:00Z",
  "bundle": "httpbis/httpbis-20260701T000000Z.tar.gz",
  "bundle_sha256": "…", "bundle_bytes": 47185920 }
```

**The payload** is one gzipped tar per corpus: `<name>/<name>-<version>.tar.gz`.
One bundle plus one manifest is a few GETs per corpus (CDN-friendly, kind to the
source) rather than the hundreds a loose file tree would cost.

The compatibility tuple — `(schema_version, embedding_model, chunker_version,
vector_dim)` — is read straight from the producer's `embeddings.db` `meta` at
publish time. It is the existing vector-compatibility gate: a client whose
configured embedding model or installed `chunker_version` does not match cannot
use the vectors and must gather cold. Publishing the *default* local model (the
free, no-API-key one everyone gets out of the box) covers the majority of clients;
remote-embed-model clients are out of scope for v1 (see Non-goals).

`version` is the gather timestamp (`last-gathered`), so a client can tell whether
its locally-held base predates a newer snapshot.

## Producer side

The publisher is **`scripts/publish_seeds.py`** — an operator script (not a
`[project.scripts]` console entry; producer-only, kept out of the installed CLI
surface and never imported by the read path). It is a **one-shot**: for each
member of the store it incremental-gathers, then publishes.

**The store is the curated list.** Membership lives in the store's `index.json`,
not in the package and not retyped each run — the store directory is the operator's
own artifact (outside the package), so curation stays operator-owned but
persistent. You bootstrap the set once and edit it occasionally:

```
python scripts/publish_seeds.py ~/seed-store --add httpbis --add tls --add quic  # bootstrap
python scripts/publish_seeds.py ~/seed-store                                      # refresh all members
python scripts/publish_seeds.py ~/seed-store --add cfrg --months 12               # add one
python scripts/publish_seeds.py ~/seed-store --remove old-bof                     # drop one
```

A bare run is the whole monthly job — gather each member on its stored window,
publish what changed, sync:

```
python scripts/publish_seeds.py ~/seed-store --prune && rsync -a ~/seed-store/ host:/var/www/seed/
```

Per member the script:

1. **gathers** it (the normal `gather.sequencer` pipeline, incremental, on its
   stored `window_months`) — `--no-gather` skips this to publish the cache as-is;
2. assembles the bundle tree (the included set above), tars + gzips it, hashes it;
3. reads `(schema_version, model, chunker_version, embed_dim)` from
   `embeddings.db` `meta`;
4. writes `<name>/manifest.json` and the bundle;
5. rebuilds the root `index.json` **last** (so it never references a missing
   bundle), then drops the superseded bundle.

"One writer to the cache" still holds: the gather step *invokes* the same pipeline
the CLI does — the publisher is not a second writer, and publishing only *reads*
the cache. And the store's own `index.json` stays the single source of truth for
what a consumer can pull, so a shipped list is neither needed nor able to drift
from what the mirror actually holds.

| Option | Effect |
|---|---|
| `corpus…` (positional, optional) | process only these members this run (default: all) |
| `--add <corpus>` `[--months N]` | add a corpus to the store (records its window; default the store's) |
| `--remove <corpus>` | drop a corpus from membership |
| `--no-gather` | publish current cache contents without re-gathering first |
| `--force` | re-bundle members already published at their current version |
| `--prune` | delete bundles/dirs for corpora no longer members (default: additive) |
| `--dry-run` | print the gather / publish / skip / prune plan; write nothing |
| `-v` | per-file progress |

**Incremental.** A member whose `last-gathered` already matches its published
`version` is bundle-skipped (`--force` overrides); the gather step is itself
incremental, so an unchanged member is cheap end-to-end.

**One model per store (guardrail).** The root `index.json` carries one
compatibility tuple, so every member must share `(schema_version, model,
chunker_version, vector_dim)`. A member embedded with a different model is refused
with a clear message rather than writing an inconsistent index — use a separate
store dir for a different model.

**Host sync is out of scope.** The script only manages a local directory; you push
it with `rsync` / `aws s3 sync` / `wrangler r2` (host-agnostic).

## Consumer side

Pulling from the mirror is a **network + write** operation, so by the project's
read-only boundary it lives on the **gather path only** — never the MCP read tools
or `ietf-llm-search`, which stay offline and untouched.

**Seeding is on by default (opt-out).** The package ships a default
`IETF_LLM_SEED_URL` pointing at the project's public mirror, so a covered corpus
starts from the seed with no configuration. This is deliberate: gather is already
the networked writer, so reaching the seed mirror on the same path is in character
— and opt-out is the only way the feature helps the median user, who will never
flip an opt-in. (Consequence: **a default mirror URL ships in the package**, so the
project commits to a stable public hosting URL; a client reaching a dead or moved
URL degrades to a cold gather, never an error — see "Failure is soft" below.)

**The seed is trusted as an authoritative fresh base.** A client uses it whenever
it offers a *fresher, compatible* base than what is on local disk — cold start and
stale-corpus refresh are the same rule, not two. A local corpus older than the
snapshot is **automatically jumped forward** to it; the user opts out, never in.

Pre-gather step in `gather/sequencer.py`:

1. **Gate.** Seeding is enabled (a URL is present and not disabled), the corpus is
   listed in the seed index, and the compatibility tuple matches the client's
   configured embedding model. Then choose the base by comparing what the seed
   offers against what is local:
   - **No local copy** → seed (cold start — the seed is trivially fresher).
   - **Local older than the snapshot** (`seed.gathered` > local `last-gathered`)
     *and* the seed is a full stand-in for the user's config (its window ≥ the
     configured window; no custom `--draft` / `--mailing-list` / `--github` /
     generative sources the seed does not cover) → **re-seed**: replace the base,
     then freshen. This is the opt-out judgment call — trust the fresher seed over
     a stale local base.
   - **Otherwise** (local is as fresh or fresher, or the seed would narrow the
     window / underserve custom sources) → skip seeding; incremental-gather as
     normal.
2. **Fetch + verify.** Download the bundle to a temp dir, verify `bundle_sha256`.
3. **Install.** Unpack and atomically install into `~/.cache/ietf-llm/<corpus>/`
   (temp + `os.replace`, via `atomicio`; a re-seed swaps the existing base aside
   and restores it on any failure, so a killed re-seed never leaves a torn tree).
   Record provenance in a `seed-source` sentinel (url + version + fetched-at)
   beside `last-gathered`, so `ietf-llm --list` can show "seeded from snapshot
   2026-07-01."
4. **Freshen.** Continue the normal incremental gather. With the snapshot in
   place, it fetches and embeds only what changed since — the delta — and
   reconciles the tree to the user's persisted config.

**Why auto re-seeding a stale corpus is safe.** The freshen pass runs the user's
own persisted config, so a re-seed loses nothing permanently: a wider window or
custom sources are reconstructed on freshen (and the result is often *cleaner*
than an incrementally-accreted old corpus). The guardrails above — seed window ≥
the configured window, custom sources covered — are not about trust; they exist so
the jump is actually *cheaper*. Re-seeding to a narrower base, or one missing the
user's custom sources, would make freshen re-fetch and re-embed the difference and
cost more than a plain incremental. Within those bounds the seed is trusted and the
base moves forward automatically. The base only ever moves **forward**: seeding
never replaces a local copy that is already fresher than the snapshot.

Disabling / overriding:

- **`--no-seed`** — skip seeding for this gather (pure incremental, or a cold
  gather if there is no local copy).
- **`IETF_LLM_SEED_URL=`** (empty), or global `config.json` `seed_url: ""` —
  disable seeding persistently. Setting it to a *different* URL points the client
  at your own mirror.
- **`--refresh-base`** — force a re-seed even when local is not stale (e.g. to
  re-pull the current snapshot over a suspect local tree).

**Transparency.** When a gather (re-)seeds, it logs it at normal verbosity —
`seeding <corpus> from <url> (snapshot 2026-07-01, replacing base gathered
2025-08-12)` — so an automatic base jump is always visible, never silent.

**Failure is soft.** Not covered, tuple mismatch, disabled, offline, or a
fetch/verify error → fall through to a normal cold gather (quietly for the common
"not covered" case). The mirror only ever accelerates; it can never fail or block
a gather, so a client always works even if the mirror is down, stale, moved, or
unreachable.

## Correctness & compatibility

- **Vectors gate on the tuple.** `(schema_version, model, chunker_version,
  vector_dim)` must match; `build_index`/`search` already discard vectors on a
  mismatch, so a wrong-tuple bundle could never silently serve bad results even if
  the gate were bypassed.
- **Content drift self-heals.** After install, the follow-on gather re-hashes
  every file; any that differ from the producer's bytes are re-embedded (fresh
  text + vectors, atomic per file) and `build_index` prunes files no longer
  eligible. So a snapshot that is slightly behind head is corrected on freshen,
  never served wrong.
- **Reader-side vs write-side.** Installing a snapshot writes the cache, so it is
  write-side by definition — but it is confined to the gather path, and the result
  is an ordinary local corpus indistinguishable from one gathered from scratch.

## Non-goals / future

- **Embeddings-only distribution.** Rejected for v1: not a usable corpus without
  `files/`, and the per-file embed-skip makes the payoff an unpredictable
  coin-flip on thread/issue-heavy corpora. Reconsider *after*
  [#183](https://github.com/mnot/ietf-llm/issues/183) (per-chunk incremental)
  shrinks the re-embed delta.
- **Multiple embedding-model variants.** v1 publishes one default-model index.
  Publishing per-model variants (so remote-embed clients benefit too) is more work
  and can come later; the format already namespaces by the tuple, so variants slot
  in without a format change.
- **Going backwards.** Seeding only ever moves the base *forward*: it never
  replaces a local copy that is fresher than the snapshot, and never auto-narrows a
  wider-window or custom-source corpus (those incremental-gather instead). Auto
  re-seeding of a *stale* compatible corpus, by contrast, is the intended default
  (see Consumer side), not a non-goal.
- **Compression.** v1 uses gzip (stdlib, zero-dep). `zstd` compresses the DB
  better but adds a dependency — revisit if bundle size warrants it.
