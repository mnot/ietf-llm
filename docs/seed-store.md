# Seed store

The **seed store** is a static, publicly hosted mirror of prebuilt corpora. A local gather seeds a
covered corpus from it and then freshens, so the client fetches and embeds only what changed since
the snapshot. Back to the [docs index](README.md).

Seeding is **on by default** for local gathers: a covered corpus seeds from
`https://seed-store.mnot.net/` unless disabled (`--no-seed`, `IETF_LLM_SEED_URL=off`). If the host
isn't serving, the gather falls back to a cold gather. The cloud backend doesn't use the seed store;
its gathers start from the corpus's published version.

## What is published

A gathered corpus ships as the pair of its `files/` tree and its `embeddings.db`.

- **Included:** `files/` (minus `raw/`), `embeddings.db` (and any WAL/SHM sidecars), `topics.json`,
  and the incremental-gather manifests (`documents.json`, `materials.json`, `last-gathered`,
  `github/`).
- **Excluded:** `files/raw/`, `imap-cache/`, and producer-local sidecars (`gather-metrics.json`,
  `seed-source`).

[Externally-sourced members](#externally-sourced-members) ship an index only.

## Format

A store is a directory any static host can serve; clients need only anonymous HTTPS GET.

```
<base>/v<schema>/index.json                      entry point and compatibility gate
<base>/v<schema>/<name>/manifest.json            one per corpus
<base>/v<schema>/<name>/<name>-<version>.tar.gz  one gzipped-tar bundle per corpus
<base>/rfcs/v<schema>/...                        the RFC full-text store, same layout
```

Each store lives under a generation segment derived from the schema version (`v11` at present), so
after a schema bump old and new clients keep seeding from their own generation. `IETF_LLM_SEED_URL`
is the **base**; clients append the generation themselves, and `publish_seeds.py` writes into the
matching subdirectory of the directory you give it.

`index.json`:

```json
{
  "format": 1,
  "generated": "2026-07-01T00:00:00Z",
  "schema_version": 11,
  "embedding_model": "sentence-transformers/BAAI/bge-small-en-v1.5",
  "chunker_version": "3",
  "vector_dim": 384,
  "corpora": [
    { "name": "httpbis", "kind": "group", "subject": "HTTP Working Group",
      "window_months": 12, "gathered": "2026-07-01T00:00:00Z",
      "version": "20260701T000000Z", "manifest": "httpbis/manifest.json",
      "bytes": 47185920 }
  ]
}
```

`manifest.json` repeats the compatibility tuple and adds `bundle`, `bundle_sha256` and
`bundle_bytes`.

**Compatibility rule.** The tuple `(schema_version, embedding_model, chunker_version, vector_dim)`
is read from the producer's `embeddings.db`. A client uses a snapshot only if all four match its own,
so a store holds one model: the default local model, which covers most clients (clients using a
remote embedding model gather cold). The publisher refuses a member on a different model rather than
write an inconsistent index. A schema bump starts a new generation, so republish every member
after one.

## Publishing a seed store

`scripts/publish_seeds.py` builds and refreshes a store from your local cache. It is an operator
script, not a console entry point.

**Prerequisites:**

- The normal `ietf-llm` install with the local embedding model (the default,
  `sentence-transformers/BAAI/bge-small-en-v1.5`).
- A static HTTPS host for the output (web server, S3/R2 bucket, GitHub Pages).
- For the `rfcs` member: `rsync` on `PATH` and about 1.5 GB of free disk (see
  [Externally-sourced members](#externally-sourced-members)).

You don't need to pre-gather: the publisher gathers each member itself.

**1. Set the membership** (persisted in the store, so done once):

```
python scripts/publish_seeds.py ~/seed-store --add httpbis --add tls --add quic
```

`--add <corpus> [--months N]` records a member and its window; `--remove <corpus>` drops one. Give
the base directory: output lands in its `v<schema>` subdirectory, and a new generation inherits the
previous one's membership.

**2. Publish.** A bare run gathers each member incrementally, bundles what changed, and rebuilds
`index.json` (written last):

```
python scripts/publish_seeds.py ~/seed-store --prune
```

| Option | Effect |
|---|---|
| `corpus…` (positional) | process only these members this run |
| `--no-gather` | publish the current cache as-is |
| `--force` | re-bundle members already at their published version |
| `--prune` | delete store directories for corpora no longer members |
| `--dry-run` | print the plan; write nothing |

**3. Host it.** Sync the base directory to any static host (`rsync -a ~/seed-store/
host:/var/www/seed/`, `aws s3 sync` for S3 or R2, or a Pages repository) and note the public base
URL. Sync the whole base so every generation and the `rfcs/` store go together. Use a dedicated
hostname so hosting can move later by repointing DNS. Cache headers:

- **Bundles** (`*-<version>.tar.gz`) are immutable: `Cache-Control: public, max-age=31536000,
  immutable`.
- **`index.json` and `manifest.json`** change on every publish: a short TTL (a few minutes), or
  `must-revalidate` with an ETag.

**4. Point clients at it:** `export IETF_LLM_SEED_URL=https://host/seed/`, or set `"seed_url"` in
`~/.config/ietf-llm/config.json`.

**5. Keep it fresh.** Run the publish on a schedule (monthly suits the 12-month window); the same
run refreshes the `rfcs` member, which rebuilds only when upstream has published a newer index:

```
0 4 1 * * python .../publish_seeds.py ~/seed-store --prune && rsync -a ~/seed-store/ host:/var/www/seed/
```

## Using a seed store

Seeding is a network and write step, so it happens on the **gather path only**; the MCP read tools
and `ietf-llm-search` stay offline. A gather uses the seed whenever it is a fresher, compatible base
than what is local:

- **No local copy:** seed.
- **Local more than 60 days behind the snapshot** (`IETF_LLM_SEED_STALE_DAYS`), and the snapshot
  covers the request (window not narrowed; no extra `--draft`, `--mailing-list` or generative
  sources): re-seed, then freshen. A corpus less stale than that gathers incrementally.
- **Otherwise:** gather incrementally.

The install verifies `bundle_sha256` and swaps the tree into `~/.cache/ietf-llm/<corpus>/`
atomically (a failed swap restores the prior corpus). The follow-on gather freshens the delta and
reconciles to your persisted config. Seeding only moves a corpus forward. Not covered, tuple
mismatch, disabled, offline and verify failure all fall through to a normal cold gather.

`ietf-llm --init` does the housekeeping on a machine that never gathers (a read-only MCP deployment,
or a first run), including installing the RFC corpus.

**Controls:**

- `--no-seed` stops seeding and persists until `--seed`. Gathers then run cold and offline, and
  `list_corpora` skips the catalog lookup.
- `--seed` re-enables it.
- `IETF_LLM_SEED_ENABLED=off` (environment) or `seed_enabled: false` (global config) toggles
  seeding without a gather; the environment wins.
- `IETF_LLM_SEED_URL=` empty or `off`, or `seed_url: ""`, disables by URL; any other value points at
  your own mirror.
- `--refresh-base` re-seeds even when the local copy isn't stale; freshening then re-fetches any
  wider window or extra sources.

**Visibility.** `gather_status` notes when a run seeded, and `list_corpora` marks a seeded corpus
`· seeded <date>`. It also lists the store's un-gathered corpora ("available to fast-start"). On a
local stdio server that catalog is cached in `_seed/index.json` and revalidated in the background at
most hourly (blocking only on the first fetch); the read-only HTTP replica never fetches it.

## Special members

### The chartered-group mirror (`_groups`)

`_groups` is a member of the main store, carrying the same compatibility tuple as any gathered
corpus. It has no gather command: the publisher refreshes it in-process, uncapped, so a snapshot
holds every charter (`--add _groups`; `--no-gather` publishes what is cached). A client installs it
only on a cold start; a machine that has a mirror refreshes it incrementally from the Datatracker.
Names starting with `_` aren't offered by `list_corpora`. See
[architecture.md](architecture.md#the-chartered-group-mirror).

### Externally-sourced members

`rfcs`, the full text of the RFC series, is assembled from the semantic index
[rfc.fyi](https://github.com/mnot/rfc.fyi) publishes plus a plain-text RFC mirror, rather than
gathered. It is published to its own store, because its chunks use rfc.fyi's chunker and can't share
a compatibility tuple with gathered corpora:

```
python scripts/publish_seeds.py ~/seed-store/rfcs --add rfcs
```

- **No gather, no window.** `--months` is meaningless; its version is the upstream build id, so an
  unchanged upstream is a no-op.
- **Index-only bundle**, with no `files/` tree.
- **Cost on first run:** about 138 MB downloaded from rfc.fyi and about 530 MB rsynced from the RFC
  Editor (the mirror defaults under the cache; `IETF_LLM_RFC_MIRROR` moves it), a corpus of about
  660 MB and a bundle of about 270 MB. Later runs download nothing unless upstream has republished.
- **Reconciliation.** The run prints `RFC text mirror: N/N RFCs match the build`. An RFC whose text
  has moved since the upstream build is dropped, so many differing RFCs means the mirror is out of
  step with the index. If neither that line nor the `rfc index:` build line appears, the member was
  skipped; the reason is in the run's `skipped` list.
- **Client side.** It isn't tied to the corpus being gathered: it installs as housekeeping once per
  `ietf-llm` run (and in `--init`), under the same opt-out and staleness margin, refreshes on the
  upstream build id, and never goes backwards. A follow-on gather never freshens it.

## Not supported

- Embeddings-only distribution of a gathered corpus (the read tools need `files/`).
- Multiple embedding models in one store.
- Replacing a fresher local copy, or narrowing a wider-window or custom-source corpus.
- Compression other than gzip.
