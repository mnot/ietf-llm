# Storage & locations

**This document is for:** deciding where ietf-llm keeps its files and how a deployment
reads them — relocating the cache/config/index onto faster or mounted volumes, or running the
replicated **cloud** backend. It is task-oriented; for *why* the storage seam is shaped this way
(the control plane, the compare-and-swap publish), see
[architecture.md](architecture.md#the-storage-seam-corpusstore-local-default-cloud-pluggable). —
Back to the [docs index](README.md).

## Which backend do you need?

| You are running | Backend | What to read |
|---|---|---|
| The CLI, or a single-box server (laptop, one VM) | **local** (default) | [Relocating the local cache](#relocating-the-local-cache) — and nothing else here. |
| A replicated fleet (many serving containers; gather by cron and/or in-session MCP) | **cloud** | [The cloud backend](#the-cloud-backend). |

The **local** backend reads and writes the cache directly: the live `<cache>/<corpus>` tree is the
only version. It is the default and needs no `IETF_LLM_STORE_*` configuration. The **cloud** backend
keeps immutable, versioned content and a small coordination layer in one S3-compatible bucket, so
every replica sees the same published version with no shared filesystem. Pick local unless you
actually have more than one box reading the same corpora.

## Relocating the local cache

By default everything lives under the usual home directories:

```
~/.cache/ietf-llm/<name>/       the gathered corpus + its embeddings.db
~/.config/ietf-llm/             config (global config.json, per-corpus dirs)
```

Each location can be pointed elsewhere with an environment variable:

| Variable | What | Default |
|---|---|---|
| `IETF_LLM_CACHE_DIR` | corpus root (the gathered files) | `~/.cache/ietf-llm` |
| `IETF_LLM_CONFIG_DIR` | config root | `~/.config/ietf-llm` |
| `IETF_LLM_INDEX_DIR` | per-corpus `embeddings.db` files | the cache root |
| `IETF_LLM_INDEX_IMMUTABLE` | read the index in SQLite immutable mode (read-only mounts) | off |

An unset (or blank) variable falls back to the default, so the local CLI is unaffected.

A read-only consumer (the MCP server when gathering is disabled, `ietf-llm-search`) only needs
`IETF_LLM_CACHE_DIR` to *exist* and be readable; gather needs it writable. The layout under each
root is the same as the defaults (`<root>/<name>/…`).

### Considerations for the index

`IETF_LLM_INDEX_DIR` exists because the index has a different access profile from the rest of the
corpus: search reads the whole of a corpus's `embeddings.db` on every query, where the corpus files
are read occasionally. Pointing the index at RAM-backed storage (tmpfs) while the corpus lives on a
normal volume is worthwhile for a busy server:

```bash
export IETF_LLM_CACHE_DIR=/data/ietf-llm        # corpus, on a normal volume
export IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm      # embeddings.db, on tmpfs
```

The index is a SQLite database in WAL mode. That constrains `IETF_LLM_INDEX_DIR`:

- **Local POSIX filesystem only** — a real disk or tmpfs. **Never** a network filesystem (NFS, SMB)
  or an object-store FUSE mount: SQLite's WAL and locking are unreliable there (corruption, or
  "database is locked"). To serve an object-stored index, materialise it to local disk first; do not
  point `IETF_LLM_INDEX_DIR` at a remote mount.
- **Writable, unless marked immutable** — a normal open creates the `-wal` / `-shm` sidecars, so it
  fails on a read-only mount. For a published index that nothing rewrites in place, set
  `IETF_LLM_INDEX_IMMUTABLE=1`: SQLite then reads the file directly, skipping the WAL and locking.
  Use it *only* when the index really is immutable (a read replica you publish and swap atomically),
  never while a gather rewrites it.

## The cloud backend

Set `IETF_LLM_STORE_BACKEND=cloud` for a replicated, ephemeral deployment — many serving
containers, gather driven by cron *and/or* by the in-session MCP tools. One S3-compatible bucket
holds everything: the immutable, versioned corpus content (`files/` + `embeddings.db`) and a small
**control plane** (the version pointer, gather lease, status, the read-path access marker, and
**per-WG config** keys).

**Per-WG config is fleet-wide automatically.** On cloud, each corpus's gather/export config lives
in the control plane (`corpora/<name>/config/<scope>`), written during a gather under that corpus's
lease and read with a plain GET. So **you do not need to share `IETF_LLM_CONFIG_DIR`** across
hosts: a host running `ietf-llm --all` re-gathers a corpus first gathered on another host with that
corpus's real sources, not an empty config. Global service config (`config.json` — models, embed
on/off) stays filesystem/env-bound, since it is what *selects* the backend; set it via environment
on each host.

### What you provision

**A bucket on an object store with conditional writes.** `IETF_LLM_STORE_URL=s3://bucket/prefix`
works against AWS S3, Cloudflare R2, or MinIO. The store **must support conditional writes**
(`If-Match` / `If-None-Match`) — that is what makes the control-plane compare-and-swap safe;
without it, two concurrent gathers could both believe they hold the lease. **Confirm this on your
target** before relying on it.

**The `s3` extra.** `pipx install 'ietf-llm[s3]'`. For a non-AWS endpoint set
`IETF_LLM_STORE_ENDPOINT_URL` (e.g. your R2 or MinIO endpoint); leave it unset for AWS.

**Credentials**, from the standard AWS environment / instance-role chain (`AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, or an attached instance role).

**An IAM policy** granting the role only what the store uses — whole-object reads/writes/deletes
under the prefix, plus bucket listing. The store performs `GetObject`, `PutObject` (the conditional
writes are `PutObject` with an `If-Match` / `If-None-Match` header — no extra permission),
`DeleteObject`, `HeadObject`, and paginated `ListBucket`. A minimal policy, scoped to one prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "IetfLlmObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::your-bucket/ietf-llm/*"
    },
    {
      "Sid": "IetfLlmList",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::your-bucket",
      "Condition": {"StringLike": {"s3:prefix": "ietf-llm/*"}}
    }
  ]
}
```

(`HeadObject` is covered by `s3:GetObject`. Match the resource ARN to the prefix in your
`IETF_LLM_STORE_URL`; drop the `s3:prefix` condition if the role owns the whole bucket. On R2 / MinIO
grant the equivalent read/write/list rights — the operations are the same.)

**Local scratch.** A replica materialises whole versions into `IETF_LLM_SCRATCH_DIR` to serve reads,
and reaps superseded versions automatically. Size it for roughly **2× the versions you actively read
per corpus**. **On tmpfs, scratch is RAM** — size the pod accordingly.

**No bucket lifecycle rule needed.** Superseded *version blobs* are reaped by the application: each
publish, after the pointer flip, deletes older versions in the object store, keeping the current
version plus the immediately-previous one (`IETF_LLM_RETAIN_VERSIONS`, default `2`) and sweeping up
any failed-publish orphan prefix at the same time. So object-store cost is bounded without an
age-based lifecycle rule — which would be unsafe here anyway (age-based, not reference-based, it
can expire the current version's blobs out from under readers if a corpus goes a long time without
a re-gather).

Keeping the *previous* version is what preserves the never-torn-read guarantee: a replica
re-resolves the pointer every `IETF_LLM_RESOLVE_TTL` (≤10s) while publishes are hours apart, so it
is at most one version behind, and the version it still believes is current must outlive the
publish that superseded it. Raise `IETF_LLM_RETAIN_VERSIONS` for more headroom (e.g. forced
back-to-back re-gathers); the floor is `1`.

### Read-path access recording

The `--all --used-within DAYS` refresh filter (see
[keeping a set fresh](gathering.md#keeping-a-set-of-corpora-fresh)) needs to know when each corpus
was last *read*. A reader records that coarsely when it resolves a corpus — at most one write per
corpus every few hours per process, regardless of query volume. Where the timestamp lives depends on
the backend:

- **local** — a `last-accessed` sentinel file beside `last-gathered` under `<cache>/<corpus>/`.
- **cloud** — a `corpora/<corpus>/access` key in the control plane (a `last-accessed` file on
  ephemeral scratch would not survive). It is last-writer-wins, like the gather-status key: the
  value is "the most recent access any replica saw", so a lost race only drops an older timestamp
  for a newer one — no compare-and-swap.

This is the **one** write the read path makes. It touches no version, blob, or pointer, so a serving
replica stays read-only with respect to corpus *content*. If you scope the serve fleet's IAM to a
read-only role, either widen it to allow `s3:PutObject` on `…/corpora/*/access` only, or leave it
read-only and set **`IETF_LLM_RECORD_ACCESS=off`** on the serve side — recording then no-ops
silently and the refresh filter falls back to gather times. (The write is best-effort regardless: a
rejected PUT never fails a read.) The cron that runs `--all` needs the normal read/write role.

### Configuration

| Variable | What | Required |
|---|---|---|
| `IETF_LLM_STORE_BACKEND` | `local` (default) or `cloud` | — |
| `IETF_LLM_RECORD_ACCESS` | record read-path access for the `--used-within` filter; `off` disables (default on) | — |
| `IETF_LLM_STORE_URL` | object-store locator `s3://bucket/prefix` — holds content **and** control (needs `[s3]`) | cloud |
| `IETF_LLM_STORE_ENDPOINT_URL` | S3 endpoint for a non-AWS service (R2, MinIO); unset = AWS | non-AWS |
| `IETF_LLM_S3_CONCURRENCY` | parallel object ops for publish / version hydration, and the boto3 pool size (default `16`, floor `1`) | — |
| `IETF_LLM_SCRATCH_DIR` | local dir to materialise versions into | cloud |
| `IETF_LLM_RESOLVE_TTL` | seconds to cache the current-version lookup **and** the per-WG config read; `0` disables (default `10`) | — |
| `IETF_LLM_RETAIN_VERSIONS` | published versions a publish keeps before reaping older blobs (default `2`, floor `1`) | — |
| `IETF_LLM_GATHER_MAX_INFLIGHT` | max gathers running concurrently — per host and fleet-wide (default `3`) | — |
| `IETF_LLM_HTTP_MAX_PER_HOST` | max gather HTTP requests in flight per host — non-datatracker (default `6`) | — |
| `IETF_LLM_HTTP_MAX_DATATRACKER` | max gather HTTP requests in flight to datatracker (default `2`) | — |

The non-secret store knobs may instead go in the global `config.json` (`store_backend`,
`store_url`, `scratch_dir`, `resolve_ttl`, `retain_versions`); the environment wins. The two
`IETF_LLM_HTTP_MAX_*` caps are environment-only. Object-store credentials are environment-only (the
AWS chain). The HTTP serve path validates all of this at boot and refuses to start if `cloud` is
under-configured — see [mcp-server.md](mcp-server.md#boot-time-config-validation).

### Gather accelerator caches

Alongside the control plane the same bucket holds the **gather accelerator caches** — not control
state, but mutable shared data that, left only on ephemeral local scratch, would be wiped on
scale-to-zero and rebuilt by re-hitting rate-limited upstreams (datatracker, GitHub REST). The cloud
backend round-trips them through the `KvStore` (`gather.cache_sync`): each is hydrated into local
scratch before a gather and persisted after. Each is scoped to the unit that already guarantees a
single writer, so the write-conflict policy falls out:

- `corpora/<name>/gather-cache/http-cache.json` — the datatracker conditional-GET/ETag store, **sharded
  per corpus**. The per-corpus gather lease already serialises writers, so it is a plain
  read-modify-write (no CAS, and no lost delta from a shared blob).
- `fleet/gather-cache/github-users.json`, `fleet/gather-cache/datatracker-github.json` — the
  corpus-independent GitHub/datatracker identity maps, **shared** at the fleet prefix. Two
  different-corpus gathers can run at once, so persist does a bounded compare-and-swap *merge*
  (lossless; the maps are append-mostly so contention is rare). `fleet/`-prefixed, like `fleet/slots`.
- `fleet/catalog/…` — the `find_efforts` effort catalog (the Datatracker group collection — the same
  rate-limited upstream as the ETag store). A **shared fleet singleton**, a directory of a few files
  (raw source slices + the derived `catalog.json`, each with its `.etag` sidecar) stored one key per
  file. Mirrored once per gather; every gather writes identical content, so **last-writer-wins** — no
  CAS, no merge. The restored `.etag` sidecars let the next gather revalidate (304) rather than
  re-fetch.

The sync is best-effort — a failure just means a cold rebuild, never a failed gather. The large
caches stay ephemeral: `imap-cache/` is genuinely per-corpus (and gathered mail is published as
version content), and `_rfc/` is deferred — it mirrors a CDN, not a rate-limited API, so
re-fetching it costs bandwidth, not API quota (`_catalog/` is the same shape but hits a
*rate-limited* API, hence included). (Issue #82.)

At a glance — the local path a gather reads/writes, the cloud key it round-trips to, and the policy;
plus what stays ephemeral:

| Local (gather scratch) | Cloud key | Policy |
|---|---|---|
| `.http-cache/<corpus>.json` | `corpora/<name>/gather-cache/http-cache.json` | per-corpus; lease-serialised RMW |
| `_github-users.json` | `fleet/gather-cache/github-users.json` | shared; bounded CAS-merge |
| `_datatracker-github.json` | `fleet/gather-cache/datatracker-github.json` | shared; bounded CAS-merge |
| `_catalog/` (`*.json` + `.etag`) | `fleet/catalog/…` (one key per file) | shared singleton; last-writer-wins |
| `_rfc/` | *(not synced)* | deferred — CDN mirror, costs bandwidth not API quota |
| `imap-cache/<wg>/…` | *(not synced)* | per-corpus; gathered mail published as version content |

The paths differ but the **granularity matches** on both backends: the ETag store is per-corpus
either way (a bound gather writes `.http-cache/<corpus>.json` — distinct from the unbound
process-default `.http-cache.json` — mirroring the per-corpus cloud key), and the fleet singletons
are one shared instance either way. The divergence is naming, not structure — so there is nothing
to migrate: an existing local cache keeps working untouched (the sync is a cloud-backend no-op on
local), and a fresh cloud deployment simply populates the keys on its first gather.

One more fleet key is **not** a gather cache: `fleet/routing/centroids.json` is the cross-corpus
**centroid routing** table the `which_corpus` tool reads (issue #116). The cloud `publish` merges
the just-published corpus's topic-map centroids into it under a per-corpus compare-and-swap
(concurrent publishers of different corpora must not clobber — same merge discipline as the
identity maps above), so a reader routes the whole fleet with one GET instead of materialising
every version to reach its `topics.json`. On the local backend there is no such key — routing scans
the local sidecars directly (`CorpusStore.routing_fleet_table` returns None there). See
`routing.py` and `docs/architecture.md`.

### Publish visibility

A new version is visible to a replica within `IETF_LLM_RESOLVE_TTL` seconds of a publish (the
publishing replica sees it at once). Raise it to cut control-plane round trips; set `0` for instant
cross-replica visibility. Because every version is immutable, a stale resolve only ever serves an
older-but-complete version — never a torn one.
