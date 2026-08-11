# Seed store

The **seed store** is a static, publicly-hosted mirror of prebuilt corpora. A
local gather seeds a covered corpus from it and then freshens, so a client only
fetches and embeds what changed since the snapshot (usually < 1 month) instead of
paying the full embed + download cost from scratch. Issue
[#182](https://github.com/mnot/ietf-llm/issues/182). Back to the
[docs index](README.md).

**Status:** implemented and **on by default** — a covered corpus seeds from
`https://seed-store.mnot.net/` unless disabled (`--no-seed`,
`IETF_LLM_SEED_URL=off`). Until that host is actually serving a store, clients
soft-fail to a cold gather, so it is harmless before the mirror is stood up. This
is for **local** users, not the cloud `CorpusStore` backend (which has its own
seeding).

## What is published

A published corpus is the **matched pair** — its `files/` tree *and* its
`embeddings.db`. The index alone is not a usable corpus (the read tools need
`files/`) and would not reliably skip re-embedding, so both ship together; the
text adds little over the vectors.

- **Included:** `files/` (minus `raw/`), `embeddings.db`, `topics.json`, and the
  incremental-gather manifests (`documents.json`, `materials.json`,
  `last-gathered`, `github/`).
- **Excluded:** `files/raw/` (not indexed), `imap-cache/` (large; already in
  `threads/`), and producer-local sidecars (`gather-metrics.json`, `seed-source`).

## Format

A directory servable by any static host; a client needs only HTTPS GET. Root
**`index.json`** is the entry point and compatibility gate:

```json
{
  "format": 1,
  "generated": "2026-07-01T00:00:00Z",
  "schema_version": 8,
  "embedding_model": "sentence-transformers/BAAI/bge-small-en-v1.5",
  "chunker_version": "2",
  "vector_dim": 384,
  "corpora": [
    { "name": "httpbis", "kind": "group", "subject": "HTTP Working Group",
      "window_months": 12, "gathered": "2026-07-01T00:00:00Z",
      "version": "20260701T000000Z", "manifest": "httpbis/manifest.json",
      "bytes": 47185920 }
  ]
}
```

Each corpus has a self-describing **`<name>/manifest.json`** (repeating the
compatibility tuple, plus `bundle` / `bundle_sha256` / `bundle_bytes`) and one
**`<name>/<name>-<version>.tar.gz`** payload — a few GETs per corpus, not the
hundreds a loose tree would cost.

The compatibility tuple `(schema_version, embedding_model, chunker_version,
vector_dim)` is read from the producer's `embeddings.db` `meta`. Vectors are only
usable by a client whose model and versions match, so a store holds **one model**;
publishing the default local model covers most clients (remote-embed clients
gather cold — see Non-goals).

## Publishing a seed store

`scripts/publish_seeds.py` builds and refreshes a store from your local cache. It
is an operator script (not a console entry; never imported by the read path).

### Prerequisites

- A machine with the normal `ietf-llm` install including the local embedding model
  (default `sentence-transformers/BAAI/bge-small-en-v1.5`) — a store holds one
  model, and the default covers the most clients.
- A static HTTPS host for the output (web server, S3/R2 bucket, GitHub Pages).

You do **not** need to pre-gather: the publisher gathers each member itself.

### 1. Bootstrap the membership

Add the corpora you want to cover. Membership persists in the store, so you only
do this once (and edit it occasionally):

```
python scripts/publish_seeds.py ~/seed-store --add httpbis --add tls --add quic
```

`--add <corpus> [--months N]` records each member's window; `--remove <corpus>`
drops one.

### 2. Publish

A bare run gathers each member (incremental, on its stored window), bundles what
changed, and rebuilds `index.json`:

```
python scripts/publish_seeds.py ~/seed-store --prune
```

| Option | Effect |
|---|---|
| `corpus…` (positional) | process only these members this run (default: all) |
| `--no-gather` | publish the current cache as-is, no re-gather |
| `--force` | re-bundle members already at their published version |
| `--prune` | delete store dirs for corpora no longer members |
| `--dry-run` | print the plan; write nothing |

The gather step invokes the normal pipeline (so "one writer to the cache" holds);
`index.json` is written last and is the sole source of truth for coverage. A
member on a different embedding model is refused rather than writing an
inconsistent index.

### 3. Host it

The store is just static files served over HTTPS — `index.json`, each
`manifest.json`, and the `.tar.gz` bundles, all fetched with anonymous GET. Sync
the directory to any static host and note its public base URL:

- **Web server** — `rsync -a ~/seed-store/ host:/var/www/seed/` → `https://host/seed/`
- **S3 + CDN** — `aws s3 sync ~/seed-store/ s3://bucket/seed/` (front with a CDN)
- **Cloudflare R2** — `aws s3 sync` against the R2 endpoint, or `wrangler r2`
- **GitHub Pages** — commit the directory to a Pages repo (fine for small stores)

No server-side logic, auth, or CORS is needed; the client only does GET. Use a
**dedicated hostname** (e.g. `ietf-llm-seeds.example.net`) rather than a path on a
larger site, so hosting can later move to a CDN or bucket by repointing DNS — the
hostname is the stable contract clients are pinned to.

**Cache headers** matter, especially behind a CDN, because the two file kinds have
opposite lifetimes:

- **Bundles** (`*-<version>.tar.gz`) are immutable (versioned filenames) →
  `Cache-Control: public, max-age=31536000, immutable`. This is what makes a CDN
  nearly free to run.
- **`index.json` / `manifest.json`** change every publish → a **short** TTL (a few
  minutes) or `must-revalidate` + ETag, so a new snapshot becomes visible promptly
  instead of being pinned to a stale index at the edge.

### 4. Point clients at it

Clients set the base URL and seeding is automatic (opt-out):

```
export IETF_LLM_SEED_URL=https://host/seed/
```

or persist it as `"seed_url"` in `~/.config/ietf-llm/config.json`. A covered
corpus is then seeded on its next `ietf-llm <corpus>`.

### 5. Keep it fresh

Membership persists, so a refresh needs no arguments — run it on a schedule
(monthly matches the ~1y window well):

```
0 4 1 * * python .../publish_seeds.py ~/seed-store --prune && rsync -a ~/seed-store/ host:/var/www/seed/
```

## Using a seed store (consumer)

Seeding is a network + write step, so it lives on the **gather path only**; the
MCP read tools and `ietf-llm-search` stay offline. It is **opt-out and on by
default**: unless disabled (see Controls), a gather reaches the public seed store
(`seed-store.mnot.net`) to fetch a covered corpus's snapshot before running. The
rule is one thing — use the seed whenever it is a fresher, compatible base than
what is local:

- **No local copy** → seed (cold start).
- **Local well behind the snapshot** (by more than ~60 days —
  `IETF_LLM_SEED_STALE_DAYS`), and the snapshot is a full stand-in (window not
  narrowed; no extra `--draft` / `--mailing-list` / generative sources) →
  **re-seed**, jumping the base forward, then freshen. A corpus only a snapshot
  period stale gathers incrementally instead — re-downloading a whole bundle to
  save embedding a delta it would gather for free is pure waste (issue #187).
- **Otherwise** (local as fresh or fresher, or the seed would narrow it) → skip;
  gather incrementally.

Install downloads the bundle, verifies its `bundle_sha256`, and atomically swaps
the tree into `~/.cache/ietf-llm/<corpus>/` (a failed swap restores the prior
corpus). The follow-on gather then freshens the delta and reconciles to the user's
persisted config, so a re-seed loses nothing permanently — the base only ever
moves **forward**.

Controls:

- **`--no-seed`** — stop seeding, and **remember it**: persists across gathers
  until `--seed`. Gathers then run fully cold and offline, and `list_corpora`
  drops the catalog lookup.
- **`--seed`** — re-enable seeding (persists); undoes `--no-seed`.
- **`IETF_LLM_SEED_ENABLED=off`** (env) or global `seed_enabled: false` — the same
  on/off toggle without a gather; env overrides the persisted flag.
- **`--refresh-base`** — re-seed even when not stale (an explicit override; pulls
  the snapshot as-is, freshen re-fetches any wider window / extra sources).
- **`IETF_LLM_SEED_URL=`** empty / `off`, or global `seed_url: ""` — disable by
  URL, or set a different URL to point at your own mirror.

**Best-effort.** Not covered, tuple mismatch, disabled, offline, or a verify
failure all fall through to a normal cold gather — the mirror only ever
accelerates, and a (re-)seed is logged so an automatic base jump is visible.

**Visibility.** A seed doesn't slow a gather, so a seeded first gather is quick
rather than a minutes-long cold fetch — the `start_gather` reply and routing
instructions say so. `gather_status` emits a note when a run seeded, and
`list_corpora` marks a seeded corpus with a trailing `· seeded <date>`, so a
client can tell a corpus was reconstituted from the mirror rather than gathered
cold. `list_corpora` also lists the store's **un-gathered** corpora ("available to
fast-start"), so a cold-start client with nothing gathered still knows what it can
pull cheaply. This is the one read-surface network touch, and it rides the same
gather gate as the live Datatracker lookups: a local stdio server (where you can
`start_gather`) refreshes the catalog **stale-while-revalidate** — it serves the
cached `_seed/index.json` and revalidates in the background (bounded + throttled to
≤1/hour), blocking only on the cold first fetch when nothing is cached yet — so a
routine `list_corpora` never stalls. The read-only HTTP replica never fetches and
omits the section (you can't gather there anyway).

## Externally-sourced members

Most members are gathered and then bundled. A member may instead be
**assembled from an upstream artifact** — today just `rfcs`, the full text of
the RFC series, built from the semantic index
[rfc.fyi](https://github.com/mnot/rfc.fyi) publishes plus a plain-text mirror
([#230](https://github.com/mnot/ietf-llm/issues/230)). Four differences:

- **No gather, no window.** `--months` is meaningless; a refresh means
  rebuilding from whatever upstream has published since.
- **The version is the upstream build id**, not a gather time. An unchanged
  upstream is a no-op end to end: same version, so the re-bundle is skipped
  exactly as for a corpus that has not moved.
- **The bundle is index-only** — no `files/` tree, because the text lives in
  the index. See the scoped non-goal below.
- **The client never freshens it.** There is no local pipeline that could,
  so a follow-on gather would be a no-op at best.

The publisher needs no extra configuration: both inputs are public, and the
~530 MB text mirror defaults under the cache (`IETF_LLM_RFC_MIRROR` moves
it). Membership is added like any other — `--add rfcs` — and the source is
implied by the name.

## Non-goals

- **Embeddings-only distribution, for a *gathered* corpus** — not usable
  without `files/`, and the per-file skip makes the saving unreliable.
  Revisit after [#183](https://github.com/mnot/ietf-llm/issues/183)
  (per-chunk incremental). This does **not** cover an externally-sourced
  member (below): both halves of that reasoning are about gathering, and
  such a member is never gathered.
- **Multiple model variants** — v1 ships one default-model store; the format
  already namespaces by the tuple, so variants slot in later.
- **Going backwards** — seeding never replaces a fresher local copy or
  auto-narrows a wider-window / custom-source corpus.
- **`zstd`** — v1 uses stdlib gzip; revisit if bundle size warrants a dependency.
