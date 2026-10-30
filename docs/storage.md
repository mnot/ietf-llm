# Storage & locations

**This document is for:** relocating the cache, config and index onto faster or mounted volumes, or
running the replicated **cloud** backend. Back to the [docs index](README.md).

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Which backend do you need?](#which-backend-do-you-need)
- [Relocating the local cache](#relocating-the-local-cache)
  - [Considerations for the index](#considerations-for-the-index)
- [The cloud backend](#the-cloud-backend)
  - [Guarantees](#guarantees)
  - [Bucket layout](#bucket-layout)
  - [What you provision](#what-you-provision)
  - [Configuration](#configuration)
  - [Read-path access recording](#read-path-access-recording)
  - [Gather accelerator caches](#gather-accelerator-caches)
  - [Failures and recovery](#failures-and-recovery)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Which backend do you need?

- **local** (default): the CLI, or a single-box server. The live `<cache>/<corpus>` tree is the only
  version; no `IETF_LLM_STORE_*` configuration is needed.
- **cloud**: a replicated fleet (many serving containers; gather by cron and/or in-session MCP).
  Immutable, versioned content and a small coordination layer live in one S3-compatible bucket, so
  every replica sees the same published version with no shared filesystem.

Use local unless more than one box reads the same corpora.

## Relocating the local cache

By default the corpus lives in `~/.cache/ietf-llm/<name>/` (gathered files and `embeddings.db`) and
config in `~/.config/ietf-llm/`. Each can move:

| Variable | What | Default |
|---|---|---|
| `IETF_LLM_CACHE_DIR` | corpus root | `~/.cache/ietf-llm` |
| `IETF_LLM_CONFIG_DIR` | config root | `~/.config/ietf-llm` |
| `IETF_LLM_INDEX_DIR` | per-corpus `embeddings.db` files | the cache root |
| `IETF_LLM_INDEX_IMMUTABLE` | read the index in SQLite immutable mode | off |

Read-only consumers (the MCP server with gathering disabled, `ietf-llm-search`) need the cache to
exist and be readable; gather needs it writable.

### Considerations for the index

A busy server can put the index on tmpfs (`IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm`) while the corpus
stays on a normal volume. The index is a SQLite database in WAL mode:

- **Local POSIX filesystem only.** Never NFS, SMB or an object-store FUSE mount; WAL and locking
  fail there (corruption, or "database is locked").
- **Writable, unless immutable.** A normal open creates `-wal` / `-shm` sidecars and fails on a
  read-only mount. For an index that nothing rewrites in place, set `IETF_LLM_INDEX_IMMUTABLE=1`;
  never while a gather rewrites it.

## The cloud backend

Set `IETF_LLM_STORE_BACKEND=cloud`. One bucket holds each corpus's versioned content (`files/` +
`embeddings.db`) and the control plane (version pointer, gather lease, status, per-WG config).

An in-session `start_gather` and the CLI (`ietf-llm <corpus>`, `--all`) run the same lifecycle: take
the corpus's lease and a fleet gather slot, seed a local workspace from the published version,
restore the accelerator caches, gather, publish a new version, persist the caches. The local cache
on a cloud host is scratch; only what is published reaches readers. Per-WG gather and export config
is kept in the bucket, so hosts need not share `IETF_LLM_CONFIG_DIR`; global config (`config.json`)
stays file/env-bound on each host.

### Guarantees

- **Atomic publish.** Content goes to a fresh prefix nothing references, and the pointer flips last.
  A reader sees the previous version or the new one whole (the previous one is retained for requests
  that resolved it just before a flip); an interrupted publish leaves it current. Replicas keep
  serving the published version throughout a gather and write no content, only the access marker.
- **One gatherer per corpus.** A gather holds the corpus's lease (1 hour TTL, renewed every 15
  minutes). At most `IETF_LLM_GATHER_MAX_INFLIGHT` gathers run across the fleet.
- **Fenced flip.** Publish flips the pointer only if the publisher still holds the lease and nobody
  has moved the pointer since it started; otherwise it publishes nothing.

### Bucket layout

```
corpora/<name>/pointer                    current version token   (compare-and-swap)
corpora/<name>/lease                      gather lease            (compare-and-swap)
corpora/<name>/status                     latest gather status
corpora/<name>/access                     last read time
corpora/<name>/config/<scope>             per-WG config
corpora/<name>/versions/<version>/...     immutable content + manifest.json
corpora/<name>/gather-cache/...           per-corpus gather caches
fleet/slots                               gather-slot semaphore   (compare-and-swap)
fleet/routing/ , fleet/gather-cache/ , fleet/catalog/    shared across corpora
```

### What you provision

- **A bucket with conditional writes** (`If-Match` / `If-None-Match`): AWS S3, Cloudflare R2 or
  MinIO, as `IETF_LLM_STORE_URL=s3://bucket/prefix`. The lease and pointer depend on conditional
  writes, so confirm your target supports them. A lost race is recognised as `412` or `409`; whether
  R2 and MinIO report one of those is unconfirmed.
- **The `s3` extra** (`pipx install 'ietf-llm[s3]'`). For a non-AWS service set
  `IETF_LLM_STORE_ENDPOINT_URL`.
- **Credentials** from the standard AWS environment or instance-role chain.
- **IAM rights** on the prefix: `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on
  `arn:aws:s3:::bucket/prefix/*`, plus `s3:ListBucket` on `arn:aws:s3:::bucket` (conditioned on
  `s3:prefix` = `prefix/*` if the role shares the bucket). R2 and MinIO need the equivalents.
- **Scratch** in `IETF_LLM_SCRATCH_DIR`, where a replica materialises the versions it serves
  (superseded ones are reaped). Size it for about twice the versions you actively read per corpus;
  on tmpfs it is RAM.
- **No bucket lifecycle rule.** Each publish deletes older versions after the flip (keeping the
  current one plus `IETF_LLM_RETAIN_VERSIONS - 1`) and any failed-publish orphans. Don't add an
  age-based expiry rule: it would delete the blobs of a current version that hasn't been
  re-gathered recently.

### Configuration

| Variable | What | Required |
|---|---|---|
| `IETF_LLM_STORE_BACKEND` | `local` (default) or `cloud` | — |
| `IETF_LLM_STORE_URL` | `s3://bucket/prefix` | cloud |
| `IETF_LLM_STORE_ENDPOINT_URL` | endpoint for a non-AWS service; unset = AWS | non-AWS |
| `IETF_LLM_SCRATCH_DIR` | local dir to materialise versions into | cloud |
| `IETF_LLM_RESOLVE_TTL` | seconds a replica caches the current-version lookup and per-WG config; `0` disables (default `10`) | — |
| `IETF_LLM_RETAIN_VERSIONS` | published versions kept when reaping (default `2`, floor `1`) | — |
| `IETF_LLM_S3_CONCURRENCY` | parallel object operations and boto3 pool size (default `16`) | — |
| `IETF_LLM_GATHER_MAX_INFLIGHT` | gathers running at once, per host and fleet-wide (default `3`) | — |
| `IETF_LLM_HTTP_MAX_PER_HOST` | gather HTTP requests in flight per host, datatracker excluded (default `6`) | — |
| `IETF_LLM_HTTP_MAX_DATATRACKER` | gather HTTP requests in flight to datatracker (default `2`) | — |
| `IETF_LLM_RECORD_ACCESS` | `off` stops recording read access (default on) | — |

The store settings other than the endpoint can also go in the global `config.json` (`store_backend`,
`store_url`, `scratch_dir`, `resolve_ttl`, `retain_versions`, `gather_max_inflight`); the
environment wins. The `IETF_LLM_HTTP_MAX_*` caps, the endpoint and credentials are environment-only.
The HTTP serve path validates all this at boot and refuses to start if `cloud` is under-configured
([mcp-server.md](mcp-server.md#boot-time-config-validation)).

A replica sees a new version within `IETF_LLM_RESOLVE_TTL` seconds of a publish; `0` gives immediate
visibility at the cost of a control-plane read per request.

### Read-path access recording

`--all --used-within DAYS` (see [keeping a set fresh](gathering.md#keeping-a-set-of-corpora-fresh))
refreshes only corpora read recently. A reader records this at most once every six hours per
corpus per process: a `last-accessed` file in `<cache>/<corpus>/` locally, the
`corpora/<corpus>/access` key on cloud. A rejected write never fails a read.

If the serve fleet's role is read-only, either allow `s3:PutObject` on `…/corpora/*/access` or set
`IETF_LLM_RECORD_ACCESS=off` on the serve side; the filter then falls back to gather times.

### Gather accelerator caches

The bucket also holds the caches that stop a scale-to-zero host re-fetching from rate-limited
upstreams once its scratch is wiped: the datatracker ETag cache (a tar per corpus), the shared GitHub
and datatracker identity maps, the `find_efforts` effort catalog, and the cached mailing-list mail
(including mail archived from any UIDVALIDITY epoch the server has since replaced). A gather
restores them first and uploads them afterwards; nothing is configured, and `--clear-cache` deletes
the stored copies too.

A failed sync costs a cold rebuild, except that stored mailing-list mail the store errors on
fetching fails the gather before any stage runs (publishing without it would drop threads). The
chartered-group mirror (`_groups/`) isn't synced: on cloud its refresh is skipped,
`get_group_info` and `search_groups` report "not gathered yet", and `find_efforts` uses the
active-only catalog.

### Failures and recovery

- **Another host holds the lease.** The CLI exits non-zero (`--all` skips the corpus) and
  `start_gather` reports *already running*. Wait and poll `gather_status`.
- **A gatherer died.** `gather_status` shows `interrupted` once its lease lapses, up to an hour after
  the last renewal. Re-run the gather then; the previous version stayed current.
- **Lease lost mid-gather** (log: *lost the lease … stopping without publishing*): a renewal was
  refused, failed three times running, or went unrenewed for three quarters of the TTL. Nothing is
  published; re-run, and check store reachability if it recurs.
- **Publish refused** (*nothing published*): the lease was taken over, or the pointer moved, during
  the upload. Re-run; the refused upload removes its own staged prefix.
- **Credential errors:** check the credential chain and that the policy grants every right above.
