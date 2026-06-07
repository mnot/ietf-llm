# Running the MCP server over HTTP

**This document is for:** running `ietf-llm-mcp` as a shared service — one process serving many
clients over HTTP, rather than a per-user stdio subprocess. It is an operator runbook: what to set,
what to run, how to check it, and what to do when it misbehaves. For *why* the server is shaped this
way, see [architecture.md](architecture.md). — Back to the [docs index](README.md).

For local use the server speaks stdio and needs none of this; see
[Running the MCP server locally](mcp-local.md).

## Quickstart

A hosted deployment is the stdio server with three things configured around it: a **remote
embeddings** endpoint (so the image carries no torch), an **HTTP transport**, and **storage** the
replica reads. A minimal run:

```bash
# embeddings: remote, no torch in the image (see models.md)
export IETF_LLM_EMBED_MODEL=openai-embed/bge-small-en-v1.5
export IETF_LLM_EMBED_BASE_URL=https://your-endpoint/v1
export IETF_LLM_EMBED_TOKEN=...

# storage: corpus mounted read-only, index on tmpfs (see storage.md)
export IETF_LLM_CACHE_DIR=/data/ietf-llm
export IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm

# transport + observability
export IETF_LLM_MCP_TRANSPORT=http
export IETF_LLM_MCP_HOST=0.0.0.0   # bind wide ONLY behind a proxy (see Deployment contract)
export IETF_LLM_LOG_FORMAT=json

ietf-llm-mcp
```

The MCP endpoint is served at `/mcp`; `GET /health` is the readiness probe. The read path touches no
network and never writes — every query opens its own read-only SQLite connection — so many clients,
and a concurrent re-gather, are safe against one corpus.

## Before you start

- **Install torch-free.** `pipx install ietf-llm` is torch-free by design: the server embeds against
  a remote endpoint, not an on-device model. For the cloud store backend add the `s3` extra:
  `pipx install 'ietf-llm[s3]'`.
- **Provide a remote embeddings endpoint.** Two read tools (`search_corpus`, `read_topic`) embed
  their query against `IETF_LLM_EMBED_BASE_URL`; everything else is deterministic. See
  [Model backends](models.md#embeddings).
- **Decide on storage.** A single replica reads a local or mounted cache; a fleet uses the cloud
  store backend. See [Storage](storage.md) — particularly the index-on-tmpfs and read-only-mount
  notes, which interact with the health probe below.
- **Gather is separate.** Corpora are gathered on the write side (`ietf-llm <name>`, where
  `IETF_LLM_CACHE_DIR` is writable); the server only reads — unless you opt into the
  [in-session gather](#in-session-gather-opt-in) tools.

## Deployment contract

Read this before exposing the HTTP server to anything but localhost.

`ietf-llm-mcp` is a read-only query surface over the *public* IETF record. It is **not designed to
sit on the open Internet.** It provides **no identity, rate limiting, or cost control** and assumes a
trust boundary you control: run it on an internal interface (the default bind is `127.0.0.1`) and put
your own proxy in front of anything wider.

**If you front it with a proxy, that proxy owns identity, rate limiting, and quota** — the server
will not do any of it, now or by configuration:

- **No authn/z.** No login, no API key, no per-client identity. Widen `IETF_LLM_MCP_HOST` to
  `0.0.0.0` only behind a proxy that authenticates.
- **No rate limiting.** Nothing caps request rate or concurrency beyond the per-call tool deadline
  (`IETF_LLM_TOOL_TIMEOUT`). One client can saturate CPU.
- **No quota or budget cap.** `search_corpus` and `read_topic` each make a metered, paid
  `/v1/embeddings` call per request — no budget ceiling, no circuit breaker, no query-embedding
  cache. A busy or hostile client runs up the embedding bill.

**What you're defending:** availability, the embedding bill, and abuse of compute — **not
confidentiality.** The read path serves only the public IETF record (already public at ietf.org), so
there is nothing secret to leak. The secrets that *do* exist (the embedding token, any object-store
credential) are read from the environment only and never written to disk or returned to a client.

The one break from "read-only" is the opt-in [in-session gather](#in-session-gather-opt-in), which
writes and reaches the network; leave it off for an exposed replica.

## Configuration reference

Every variable the HTTP serve path reads, by concern. Storage and embedding variables are documented
in [Storage](storage.md) and [Model backends](models.md); the serve-specific ones are below.

**Transport**

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_MCP_TRANSPORT` | `stdio` or `http` | `stdio` |
| `IETF_LLM_MCP_HOST` | bind address (widen only behind a proxy) | `127.0.0.1` |
| `IETF_LLM_MCP_PORT` | bind port | `8000` |
| `IETF_LLM_MCP_STATELESS` | stateless sessions (`0`/`false` for stateful) | `1` (on) |
| `IETF_LLM_MCP_ALLOWED_HOSTS` | comma-separated `Host` allow-list (enables DNS-rebinding protection) | unset (off) |
| `IETF_LLM_MCP_ALLOWED_ORIGINS` | comma-separated `Origin` allow-list (browser callers) | unset (any) |

**Observability**

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_LOG_FORMAT` | `text` or `json` | `text` |
| `IETF_LLM_TOOL_TIMEOUT` | per-tool-call deadline, seconds (`0` disables) | `120` |
| `IETF_LLM_DEBUG_LOG` | per-request timing telemetry | off |

**In-session gather** (opt-in; see [that section](#in-session-gather-opt-in))

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_ENABLE_GATHER` | register the gather tools (writes + network) | off |
| `IETF_LLM_GATHER_MAX_INFLIGHT` | max gathers concurrently — per host and fleet-wide | `3` |
| `IETF_LLM_GATHER_QUEUE_MAX` | max queued gathers before `start_gather` is refused | `16` |
| `IETF_LLM_HTTP_MAX_DATATRACKER` | max gather HTTP requests in flight to datatracker | `2` |
| `IETF_LLM_HTTP_MAX_PER_HOST` | max gather HTTP requests in flight per other host | `6` |

The MCP endpoint is at `/mcp`. Run wide behind a proxy:

```bash
IETF_LLM_MCP_TRANSPORT=http IETF_LLM_MCP_HOST=0.0.0.0 ietf-llm-mcp
```

### Stateless sessions

The HTTP transport runs **stateless by default**: the server keeps no `Mcp-Session-Id` state between
requests, so any replica behind a load balancer can answer any request with no session affinity — the
right shape for this read-mostly server. Set `IETF_LLM_MCP_STATELESS=0` (or `false`/`no`/`off`) for
stateful per-client sessions. The setting is ignored by the stdio transport. The boot posture banner
reports the effective value.

### Host / Origin allow-list

By default the server does **no** `Host`-header validation: it assumes a proxy or firewall in front
(the intended production shape). If you instead expose it directly, set `IETF_LLM_MCP_ALLOWED_HOSTS`
to the exact public host(s) you serve under — this turns on DNS-rebinding protection, and any request
whose `Host` is not on the list is rejected with `421 Misdirected Request`. Each entry is an exact
`host` / `host:port`, or a `host:*` wildcard (e.g. `mcp.example.org,localhost:*`).
`IETF_LLM_MCP_ALLOWED_ORIGINS` similarly restricts the browser `Origin` header. The boot banner
reports the effective `host_allowlist` (`off` when unset).

## Boot-time config validation

The HTTP serve path validates its configuration **before binding**, so a contradictory or
under-provisioned config fails fast at boot rather than minutes into a gather or on the first
`search_corpus`. It validates cross-knob *consistency*:

- **Hard refuse** (logs the reason, exits 1) for configs that cannot work:
  - `IETF_LLM_ENABLE_GATHER` together with `IETF_LLM_INDEX_IMMUTABLE` — gather must write the index
    the mount marks read-only.
  - gather with a local torch-backed embed model on a torch-free image and no `--no-embed` — the
    embed step would crash mid-pipeline.
  - a remote `openai-embed/...` model with no `IETF_LLM_EMBED_BASE_URL` — the read path would fail at
    request time.
  - `IETF_LLM_STORE_BACKEND=cloud` under-configured (missing `IETF_LLM_STORE_URL` /
    `IETF_LLM_SCRATCH_DIR`), or an unrecognised backend name.
- **Warn but never block** when the bind host is non-loopback — the no-auth posture is the operator's
  risk to own; the warning is louder when gather is also on.
- **Always** log a one-line posture banner (transport, bind, gather on/off, embed backend, embed
  model, index dir, immutable, store backend), honouring `IETF_LLM_LOG_FORMAT=json`.

## Health

`GET /health` is a readiness probe for a load balancer or orchestrator. It returns `200` once the
index directory is mounted **and a corpus index actually opens** — the probe runs a trivial read
against one `embeddings.db`, so an index that is present but unservable (e.g. a WAL database on a
read-only mount without `IETF_LLM_INDEX_IMMUTABLE`, or a truncated file) fails the probe instead of
passing it. A server with no corpora gathered yet is still ready. `503` otherwise, with a small JSON
body (an `index_probe` field reports `ok` / `no-corpora` / `failed`). It makes **no** upstream call —
a slow or unreachable embedding endpoint won't flap readiness — so it reports "configured and ready
to serve", not "the backend answered".

The JSON body also carries two operator-facing fields that don't gate readiness:

- `version` — the running package version, for correlating behaviour across a rolling deploy.
- `corpora` — a bounded freshness summary read from the per-corpus `last-gathered` sentinels (no
  upstream call): `count` (all cached corpora), `tracked` (those carrying a sentinel — caches
  predating freshness tracking have none), and `oldest` / `newest`, each
  `{corpus, last_gathered, age_seconds}` (or `null` when nothing is tracked). `oldest` is the
  staleness floor — a replica can be perfectly ready while serving a corpus that went stale days ago,
  and this is how an operator sees that at a glance.

`GET /metrics` exposes a Prometheus scrape (RED per tool, the embedding backend's call/error/latency,
and a per-corpus `last-gathered` age gauge); it is read-only and zero-dependency.

## Cache freshness and degraded mode

**The serve process never writes the cache.** `gather` is the only writer (`ietf-llm <name>`), run
out-of-band on a write node; a serving replica only ever reads. Fresh data reaches a replica in three
steps:

1. Gather on the write side, producing the corpus tree and its `embeddings.db`.
2. Publish those to the storage the replica reads — a shared mount, or a sync to local disk. Corpus
   writes are atomic (temp + rename), so a reader sees the old bytes or the new, never a torn file.
   The index is SQLite: publish it immutable and swap it atomically, then read with
   `IETF_LLM_INDEX_IMMUTABLE=1` on a read-only mount (see [Storage](storage.md)).
3. The replica picks it up with no restart — every tool opens a fresh read-only connection per call.

The **cloud store backend** (`IETF_LLM_STORE_BACKEND=cloud`, see
[Storage](storage.md#the-cloud-backend)) does steps 2–3 for you: a publish is visible fleet-wide with
no separate sync and no torn read. It is also what makes the
[in-session gather](#in-session-gather-opt-in) durable and fleet-coherent rather than a single-box
convenience.

**Degraded mode when the embedding upstream is down.** Only two tools embed their query, and they are
the only ones that fail if the remote `/v1/embeddings` endpoint is unreachable:

- **Fail:** `search_corpus` and `read_topic`.
- **Keep working:** every deterministic tool — `overview`, `read_digest`, `list_corpora`,
  `list_files`, `list_labels`, `find_citations`, `find_replies`, `tally_positions`, `rfc_search` /
  `get_rfc`, `read_file_section`, `get_chunk_text` / `get_chunks_batch`, and `fetch_by_url` (which
  fetches a URL rather than embedding — independent of the embedding backend, but not of network
  egress).

`GET /health` makes no upstream call, so a down embedding endpoint degrades search without flapping
readiness.

## Logging

Set `IETF_LLM_LOG_FORMAT=json` for one-line structured records (`ts` / `level` / `msg`) a collector
can ingest; the default is human-readable text. Logs go to stderr (stdout is reserved for the stdio
protocol; container runtimes capture stderr) and carry no secrets. `IETF_LLM_DEBUG_LOG=1` additionally
records per-request timing telemetry. When serving over HTTP, the server emits a one-line startup
preamble at this same level — version, bind address, and the `corpora` freshness floor — mirroring
`/health`, so a deploy log shows which build a replica came up on and how stale its caches were at
boot.

## In-session gather (opt-in)

The server is read-only by default. Setting `IETF_LLM_ENABLE_GATHER=1` registers two extra tools so a
client can gather a new corpus without dropping to a shell:

- `start_gather(corpus, [mailing_list], [draft], [github], [author], [new_drafts], …)` — enqueues a
  gather and returns immediately. The corpus shape (group / list / custom / synthetic) is inferred
  from the arguments.
- `gather_status([corpus])` — reports `queued`, `running` (with the current stage, e.g.
  `stage 7/17 (github issues)`, and elapsed time), `done`, `failed` (with the error), or `interrupted`
  (the gatherer ended before completion). Status is persisted to `<corpus>/gather-status.json` and,
  on the cloud backend, to the control plane so any replica can report it.

**This is the one break from the read-only / no-network contract.** Leave it **off** for a shared
HTTP replica or a read-only-mounted cache *on the local backend*, where a gathered corpus is only
durable on the box that wrote it. On the **cloud backend** it is a first-class shape: the gather
publishes a new immutable version through the store (atomic pointer flip, visible to every replica)
under a cross-host lease, so enabling it on a replicated fleet is safe — see
[Cache freshness](#cache-freshness-and-degraded-mode). If you enable it on the torch-free serve image,
use a remote `openai-embed/...` embedding model so the gather's index build pulls no torch.

### Gather concurrency

Gathers run capped to a few at once, to stay polite to shared upstreams (datatracker, mailarchive,
GitHub) without making a second client wait behind the first. `IETF_LLM_GATHER_MAX_INFLIGHT`
(default `3`) sets the cap two ways:

- **Per host:** a pool of that many workers; beyond it, requests queue (FIFO).
  `IETF_LLM_GATHER_QUEUE_MAX` bounds the backlog (default `16`); past it, `start_gather` is refused
  rather than piling up unbounded work.
- **Fleet-wide:** each running gather holds one of N per-job slots in the control plane, so the whole
  deployment runs at most N at once. The slot only applies on the **cloud backend**; on the local
  backend the per-host worker pool is the bound.

Set it to `1` for strictly serial gathering, or higher for more throughput at the cost of more
concurrent upstream load. A request for a corpus already in flight (here or on another host) reports
*already running*; a request for a different corpus when no slot is free is accepted as `queued`.
Either way the client polls `gather_status` — it does not retry. The per-corpus lease is taken at
enqueue, so the same corpus is never gathered twice across the fleet, and a `queued`/`running` record
whose gatherer died is relabelled `interrupted` once its lease lapses.

> The CLI (`ietf-llm <corpus>`) does **not** yet participate in the fleet-wide slot, so a cron/shell
> gather runs alongside the cap rather than counting toward it. Bringing the CLI under the same slot
> is a planned follow-up.

### Per-host request governor

The gather-count cap above bounds *how many gathers* run; a separate, finer cap bounds *how many HTTP
requests* any one gather (or all of them in a process) keeps in flight to a single host. Every gather
fetch routes through a per-host slot pool, so a wide intra-gather fan-out — the draft / RFC text
downloads run in parallel — can never exceed the budget for a host regardless of pipeline structure
or concurrent gathers.

- `IETF_LLM_HTTP_MAX_DATATRACKER` (default `2`) — cap for `datatracker.ietf.org`, kept tight because
  it is a shared, database-backed community service.
- `IETF_LLM_HTTP_MAX_PER_HOST` (default `6`) — cap for every other host (the CDN-fronted draft / RFC
  text hosts, GitHub, the mirrors), which tolerate a wider fan-out.

Lowering them makes gathers gentler (and slower); raising the per-host cap speeds up the parallel
document downloads against the static hosts.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Process exits 1 immediately at boot, logging a config reason | Cross-knob contradiction caught by [boot validation](#boot-time-config-validation) | The log names it — e.g. `ENABLE_GATHER`+`INDEX_IMMUTABLE`, a remote embed model with no `EMBED_BASE_URL`, or `cloud` missing `STORE_URL`/`SCRATCH_DIR`. Fix that knob. |
| `GET /health` returns `503` with `index_probe: failed` | A WAL `embeddings.db` on a read-only mount, or a truncated/unservable index file | Set `IETF_LLM_INDEX_IMMUTABLE=1` for a read-only mount; otherwise re-publish the index. The index must be on a local POSIX FS, never NFS/FUSE (see [Storage](storage.md#where-the-index-can-live-it-is-sqlite)). |
| `GET /health` returns `200` but `index_probe: no-corpora` | No corpus gathered yet | Expected — the replica is ready; gather a corpus on the write side. |
| `search_corpus` / `read_topic` error, every other tool works | The remote `/v1/embeddings` endpoint is unreachable | [Degraded mode](#cache-freshness-and-degraded-mode) — check `IETF_LLM_EMBED_BASE_URL` reachability and the token; deterministic tools keep serving meanwhile. |
| Embedding bill climbing unexpectedly | No built-in quota — every `search_corpus`/`read_topic` is a paid call (by design) | Front the server with a proxy that rate-limits and caps spend; see [Deployment contract](#deployment-contract). |
| A replica serves stale corpus content | Stale resolve cache, or no publish happened | On cloud, lower `IETF_LLM_RESOLVE_TTL` (the `oldest` field in `/health` shows the staleness floor); confirm the write-side gather published. On local, confirm the sync/mount updated. |
| `start_gather` returns *already running* | A gather for that corpus is in flight here or on another host (per-corpus lease) | Poll `gather_status`; do not retry. A dead gatherer's record flips to `interrupted` once its lease lapses. |
| `start_gather` refused (queue full) | Backlog hit `IETF_LLM_GATHER_QUEUE_MAX` | Wait and retry, or raise the cap / `IETF_LLM_GATHER_MAX_INFLIGHT` if upstreams tolerate it. |
| Requests rejected with `421 Misdirected Request` | `IETF_LLM_MCP_ALLOWED_HOSTS` is set and the request `Host` isn't on the list | Add the public host (`host`, `host:port`, or `host:*`) to the allow-list, or unset it if a proxy already validates `Host`. |
| Cloud store errors mention rejected credentials | Missing/invalid AWS credentials, or the IAM policy lacks an operation | Check `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (or the instance role) and that the policy grants `GetObject`/`PutObject`/`DeleteObject` on the prefix plus `ListBucket` (see [Storage](storage.md#what-you-provision)). |
| Two gathers seem to clobber each other on cloud | The bucket doesn't honour conditional writes (`If-Match`/`If-None-Match`) | The control-plane CAS needs them — verify your object store supports them (notably R2); if not, it can't back the cloud control plane. |
