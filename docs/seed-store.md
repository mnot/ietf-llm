# Seed store

The **seed store** is a static, publicly-hosted mirror of prebuilt corpora. A
local gather seeds a covered corpus from it and then freshens, so a client only
fetches and embeds what changed since the snapshot (usually < 1 month) instead of
paying the full embed + download cost from scratch. Issue
[#182](https://github.com/mnot/ietf-llm/issues/182). Back to the
[docs index](README.md).

**Status:** implemented, but off by default until a **default hosting URL** is
chosen (`config.service._DEFAULT_SEED_URL` is `None`); until then seeding runs
only when an operator sets `IETF_LLM_SEED_URL`. This is for **local** users, not
the cloud `CorpusStore` backend (which has its own seeding).

## What is published

A published corpus is its **`files/` tree and its `embeddings.db` together**, not
the index alone. `embeddings.db` records a per-file content SHA-256, and a
re-gather skips embedding any file whose bytes still match — so shipping the exact
`files/` the vectors were built from makes that skip deterministic. Independent
gathers do *not* reproduce byte-identical `files/` (thread/issue rendering depends
on time-varying Datatracker identity data), and the read tools need `files/`
anyway, so an embeddings-only ship would be neither usable nor a reliable saving.
The `files/` text is mostly redundant with the DB's chunk text, so it adds little
over the index the vectors already dominate.

- **Included:** `files/` (minus `raw/`), `embeddings.db`, `topics.json`, and the
  incremental-gather manifests (`documents.json`, `materials.json`,
  `last-gathered`, `github/`).
- **Excluded:** `files/raw/` (not indexed), `imap-cache/` (large, already
  materialised in `threads/`), and producer-local sidecars
  (`gather-metrics.json`, `seed-source`, …).

## Format

A directory servable by any static host (web server, R2/S3 public bucket, GitHub
Pages); a client needs only HTTPS GET. Root **`index.json`** is the entry point
and compatibility gate:

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

## Producer

`scripts/publish_seeds.py` is an operator script (not a console entry; never
imported by the read path). A one-shot: for each member it incremental-gathers,
then bundles and publishes. Membership lives in the store (`--add`/`--remove`), so
a bare run refreshes what's already there.

```
python scripts/publish_seeds.py ~/seed-store --add httpbis --add tls   # bootstrap
python scripts/publish_seeds.py ~/seed-store --prune                   # monthly: gather + publish
rsync -a ~/seed-store/ host:/var/www/seed/                             # then sync (out of scope)
```

| Option | Effect |
|---|---|
| `corpus…` (positional) | process only these members this run (default: all) |
| `--add <corpus>` `[--months N]` | add a corpus (records its window) |
| `--remove <corpus>` | drop a corpus from membership |
| `--no-gather` | publish the current cache as-is, no re-gather |
| `--force` | re-bundle members already at their published version |
| `--prune` | delete store dirs for corpora no longer members |
| `--dry-run` | print the plan; write nothing |

The gather step invokes the normal pipeline, so "one writer to the cache" still
holds. `index.json` is written last (never references a missing bundle) and is the
sole source of truth for coverage. A member on a different embedding model is
refused rather than writing an inconsistent index.

## Consumer

Seeding is a network + write step, so it lives on the **gather path only**; the
MCP read tools and `ietf-llm-search` stay offline. It is **opt-out**: when
`IETF_LLM_SEED_URL` is set, a gather seeds before running. The rule is one thing —
use the seed whenever it is a fresher, compatible base than what is local:

- **No local copy** → seed (cold start).
- **Local older than the snapshot**, and the snapshot is a full stand-in (window
  not narrowed; no extra `--draft`/`--mailing-list`/generative sources) →
  **re-seed**, jumping the base forward, then freshen.
- **Otherwise** (local as fresh or fresher, or the seed would narrow it) → skip;
  gather incrementally.

Install downloads the bundle, verifies its `bundle_sha256`, and atomically swaps
the tree into `~/.cache/ietf-llm/<corpus>/` (a failed swap restores the prior
corpus). A `seed-source` sentinel records provenance. The follow-on gather then
freshens the delta and reconciles to the user's persisted config, so a re-seed
loses nothing permanently — the base only ever moves **forward**.

Controls:

- **`--no-seed`** — cold gather for this run.
- **`--refresh-base`** — re-seed even when not stale (an explicit override; pulls
  the snapshot as-is, freshen re-fetches any wider window / extra sources).
- **`IETF_LLM_SEED_URL=`** empty / `off`, or global `seed_url: ""` — disable; a
  different URL points at your own mirror.

**Best-effort.** Not covered, tuple mismatch, disabled, offline, or a verify
failure all fall through to a normal cold gather — the mirror only ever
accelerates, and a (re-)seed is logged so an automatic base jump is visible.

## Non-goals

- **Embeddings-only distribution** — not usable without `files/`, and the
  per-file skip makes the saving unreliable. Revisit after
  [#183](https://github.com/mnot/ietf-llm/issues/183) (per-chunk incremental).
- **Multiple model variants** — v1 ships one default-model store; the format
  already namespaces by the tuple, so variants slot in later.
- **Going backwards** — seeding never replaces a fresher local copy or
  auto-narrows a wider-window / custom-source corpus.
- **`zstd`** — v1 uses stdlib gzip; revisit if bundle size warrants a dependency.
