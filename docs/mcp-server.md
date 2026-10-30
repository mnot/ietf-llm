# Running the MCP server over HTTP

**This document is for:** running `ietf-llm-mcp` as a shared service — one process serving many
clients over HTTP, rather than a per-user stdio subprocess. For the design, see
[architecture.md](architecture.md). — Back to the [docs index](README.md).

For local use the server speaks stdio and needs none of this; see
[Running the MCP server locally](mcp-local.md).

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Quickstart](#quickstart)
- [Before you start](#before-you-start)
- [Deployment contract](#deployment-contract)
- [Configuration reference](#configuration-reference)
- [Boot-time config validation](#boot-time-config-validation)
- [Health and metrics](#health-and-metrics)
- [Logging](#logging)
- [Limits](#limits)
- [Sizing the container](#sizing-the-container)
- [Cache freshness and degraded mode](#cache-freshness-and-degraded-mode)
- [In-session gather](#in-session-gather)
- [Troubleshooting](#troubleshooting)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Quickstart

A hosted deployment is the stdio server with a remote embeddings endpoint, the HTTP transport, and
storage the replica reads. `ietf-llm-mcp` takes no flags; everything is environment variables.

```bash
# embeddings: remote, no torch in the image
export IETF_LLM_EMBED_MODEL=openai-embed/bge-small-en-v1.5
export IETF_LLM_EMBED_BASE_URL=https://your-endpoint/v1
export IETF_LLM_EMBED_TOKEN=...

# storage: corpus mounted read-only, index on tmpfs
export IETF_LLM_CACHE_DIR=/data/ietf-llm
export IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm

# transport + observability
export IETF_LLM_MCP_TRANSPORT=http
export IETF_LLM_MCP_HOST=0.0.0.0   # bind wide ONLY behind a proxy (see Deployment contract)
export IETF_LLM_LOG_FORMAT=json

ietf-llm-mcp
```

The MCP endpoint is `/mcp`; `GET /health` is the readiness probe and `GET /metrics` the Prometheus
scrape.

## Before you start

- **Install torch-free:** `pipx install ietf-llm`, plus the `s3` extra for the cloud store backend
  (`pipx install 'ietf-llm[s3]'`).
- **Embeddings** are remote; see [Model backends](models.md). Only the tools named under
  [degraded mode](#cache-freshness-and-degraded-mode) embed their query.
- **Storage:** a single replica reads a local or mounted cache; a fleet uses the cloud store
  backend. See [Storage](storage.md), including its notes on tmpfs indexes and read-only mounts.
- **Gather on a write node** (`ietf-llm <name>`, with a writable `IETF_LLM_CACHE_DIR`); the server
  only reads, unless [in-session gather](#in-session-gather) is enabled.
- **Run `ietf-llm --init` on the write side.** The RFC tools belong to no corpus and have no data
  until it is installed ([why, and what it fetches](gathering.md)). `--init` installs it into the
  `IETF_LLM_CACHE_DIR` the server reads, then exits. It needs a writable cache, so run it before the
  mount is sealed or against the cache you then publish.

## Deployment contract

`ietf-llm-mcp` is a read-only query surface over the *public* IETF record. It is not designed to sit
on the open Internet: it has no identity, rate limiting or cost control, and assumes a trust boundary
you control. The default bind is `127.0.0.1`; a proxy in front of anything wider owns identity, rate
limiting and quota.

- **No authentication.** Widen `IETF_LLM_MCP_HOST` only behind a proxy that authenticates. Boot logs
  a warning for a non-loopback bind.
- **No rate limiting.** Only the per-call deadline and the `grep_corpus` scan pool (see
  [Limits](#limits)) bound load. One client can saturate CPU, or hold every scan slot and keep
  `grep_corpus` saturated; other tools are unaffected.
- **No quota.** `search_corpus`, `read_topic`, `search_groups` and `which_corpus` each make a metered
  `/v1/embeddings` call, with no budget cap, circuit breaker or query-embedding cache.
- **No memory limit on regex scans** (see [Sizing the container](#sizing-the-container)).

## Configuration reference

Variables the serve path reads. Storage and embedding variables are in [Storage](storage.md) and
[Model backends](models.md).

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_MCP_TRANSPORT` | `stdio` or `http` | `stdio` |
| `IETF_LLM_MCP_HOST` | bind address | `127.0.0.1` |
| `IETF_LLM_MCP_PORT` | bind port | `8000` |
| `IETF_LLM_MCP_STATELESS` | stateless sessions; `0`/`false`/`no`/`off` for stateful | on |
| `IETF_LLM_MCP_ALLOWED_HOSTS` | comma-separated `Host` allow-list; setting it turns on DNS-rebinding protection | unset (off) |
| `IETF_LLM_MCP_ALLOWED_ORIGINS` | comma-separated `Origin` allow-list; applies only with `ALLOWED_HOSTS` | unset (any) |
| `IETF_LLM_SYSTEM_TRUST_STORE` | `off` verifies outbound TLS against certifi instead of the OS store, for an image without `ca-certificates` | unset (OS store) |
| `IETF_LLM_LOG_FORMAT` | `text` or `json` | `text` |
| `IETF_LLM_LOG_LEVEL` | `quiet`, `status` or `verbose` | `status` on HTTP, `quiet` on stdio |
| `IETF_LLM_DEBUG_LOG` | per-request timing telemetry; registers `get_session_log` | off |
| `IETF_LLM_TOOL_TIMEOUT` | per-tool-call deadline, seconds; `0` disables | `120` |
| `IETF_LLM_SCAN_CONCURRENCY` | worker slots for `grep_corpus` | `4` |
| `IETF_LLM_ROUTING_MIN_SCORE` | `which_corpus` abstention floor; recalibrate with `scripts/calibrate_routing.py` after changing the embedding model | `0.30` |
| `IETF_LLM_ENABLE_GATHER` | register the gather tools and live Datatracker lookups; overrides the transport default | on for stdio, off for HTTP |
| `IETF_LLM_GATHER_MAX_INFLIGHT` | gathers at once, per host and (cloud) fleet-wide | `3` |
| `IETF_LLM_GATHER_QUEUE_MAX` | queued plus running gathers per host before `start_gather` is refused | `16` |
| `IETF_LLM_GATHER_MAX_WAIT` | seconds `start_gather` / `gather_status` block; `0` never blocks | `10` |
| `IETF_LLM_GATHER_MIN_INTERVAL` | hours within which a corpus counts as fresh and is not re-gathered; `0` disables | `6` |
| `IETF_LLM_HTTP_MAX_DATATRACKER` | gather requests in flight to datatracker | `2` |
| `IETF_LLM_HTTP_MAX_PER_HOST` | gather requests in flight to each other host | `6` |

**Sessions.** The transport is stateless by default: no `Mcp-Session-Id` state is kept, so any
replica behind a load balancer can answer any request. `IETF_LLM_MCP_STATELESS=0` selects stateful
sessions, which need session affinity. The server speaks the 2026-07-28 MCP revision (always
stateless) and still answers `initialize` for older clients; a fronting proxy must pass the
`Mcp-Method` and `Mcp-Name` headers that revision adds.

**Host / Origin.** With `IETF_LLM_MCP_ALLOWED_HOSTS` unset the server does no `Host` validation and
relies on the proxy. Set it when exposing the server directly; a request whose `Host` is not listed
gets `421 Misdirected Request`. Entries are `host`, `host:port` or `host:*`
(for example `mcp.example.org,localhost:*`).

## Boot-time config validation

The serve path validates its configuration before binding and logs a one-line `serve posture:`
banner (transport, bind, stateless, gather, embed backend and model, index directory, immutable,
store backend, host allow-list, log level). It exits 1, logging the reason, when:

- `IETF_LLM_ENABLE_GATHER` and `IETF_LLM_INDEX_IMMUTABLE` are both set.
- Gather is enabled with a local `sentence-transformers/` model, no importable torch, and no
  `IETF_LLM_NO_EMBED`.
- A remote `openai-embed/...` model has no `IETF_LLM_EMBED_BASE_URL`.
- `IETF_LLM_STORE_BACKEND=cloud` lacks `IETF_LLM_STORE_URL` or `IETF_LLM_SCRATCH_DIR`, or the
  backend name is unrecognised.

## Health and metrics

`GET /health` returns `200` when the index directory is readable and one corpus `embeddings.db`
opens, and `503` otherwise. A server with no corpora is ready. It makes no upstream call, so a down
embedding endpoint does not flap readiness. The JSON body carries:

- `status` (`ok` or `unavailable`), `index_probe` (`ok`, `no-corpora` or `failed`),
  `index_dir_usable`, `embed_endpoint_configured` and `version`.
- `gathers_inflight` — in-session gathers running now. A hosting layer with an idle timeout can read
  this to keep the container awake; prefer it to parsing `/metrics`.
- `corpora` — `count`, `tracked` (those with a `last-gathered` sentinel), and `oldest` / `newest`,
  each `{corpus, last_gathered, age_seconds}` or `null`. `oldest` exposes a stale corpus on an
  otherwise healthy replica.

The probe's store reads (an S3 LIST on the cloud backend) run off the event loop on a separate
worker pool. The freshness sweep behind `/health` and `/metrics` is shared and reused for 5 seconds,
and a probe arriving during a refresh, or after a failed one, is answered from the previous sweep.

`GET /metrics` is a Prometheus text scrape (series prefixed `ietf_llm_`):

- Per tool: `tool_requests_total`, `tool_errors_total`, `tool_timeouts_total`,
  `tool_latency_seconds` (buckets reach the 120 s deadline).
- `grep_scan_stops_total` — regex scans that hit their budget or failed.
- `embed_requests_total`, `embed_errors_total`, `embed_latency_seconds`.
- Per store operation: `store_requests_total`, `store_errors_total`, `store_latency_seconds`; this is
  where the cloud backend's object-store latency shows.
- `gathers_inflight`, `gathers_started_total`, `gathers_total{state}`, `gather_duration_seconds`.
- `inflight_requests`, `build_info{version}`, `corpus_last_gathered_age_seconds{corpus}`.

## Logging

Logs go to stderr and carry no secrets; `IETF_LLM_LOG_FORMAT=json` gives one record per line (`ts`,
`level`, `msg`, plus record-specific fields). Startup logs the version, bind address and `corpora`
freshness floor. At `status` (the HTTP default) each tool call logs an access record with
`event=tool_call`, `tool`, `status` (`ok`, `timeout` or `exception`) and `duration_ms`; `verbose`
adds gather progress notes. `IETF_LLM_DEBUG_LOG=1` writes per-request timing to a JSONL file in
`_debug/` under the cache directory.

## Limits

- **Tool deadline.** A call running past `IETF_LLM_TOOL_TIMEOUT` (120 s) returns a retryable error.
- **`grep_corpus` regex scans.** A literal pattern scans in-process. A regex runs in a child process
  with a 20 s budget (also for the RFC series), killed 3 s later if it has not stopped; the reply
  then begins `INCOMPLETE SCAN` and must not be read as a negative result. At most
  `IETF_LLM_SCAN_CONCURRENCY` scans run at once, in a pool separate from the other tools.
- **`grep_corpus` output.** At most 200 matching lines are rendered per call; the totals stay
  complete.
- **`read_file_section`.** 400 lines by default, 5000 at most, and about 40,000 characters. A cut read
  carries a `PARTIAL READ` banner at both ends naming the `start_line` to continue from; `get_draft`
  and `get_issue` use the same banner and budget.
- **`search_rfc_text`.** At most 50 RFCs per call and about 40,000 characters; a capped reply says
  `PARTIAL RESULT` and how many sections it left out.
- **Gather tools.** `start_gather` and `gather_status` block at most `IETF_LLM_GATHER_MAX_WAIT`
  seconds; some MCP clients degrade when a call stays outstanding much past 10 s.

## Sizing the container

No baseline figure is given here; measure resident size with your corpora loaded, then add:

- **Index on tmpfs.** `IETF_LLM_INDEX_DIR=/dev/shm/...` counts against container memory, as does the
  cloud backend's scratch directory when it is on tmpfs (see [Storage](storage.md)).
- **Regex scans.** The scan child has no memory limit; one pathological pattern over one very long
  line has been seen at about 1.5 GB resident. Allow for `IETF_LLM_SCAN_CONCURRENCY` of them at once.
- **In-session gather.** Each running gather is a child process at lowered scheduling priority;
  allow for `IETF_LLM_GATHER_MAX_INFLIGHT` of them.
- **RFC data.** About 650 MB on disk once installed ([Gathering](gathering.md)); an RFC-series
  `grep_corpus` scan reads about 400 MB of it.

## Cache freshness and degraded mode

The serve process does not write the cache unless in-session gather is on. Fresh data reaches a
replica by gathering on the write side, then publishing the corpus tree and `embeddings.db` to the
storage the replica reads (a shared mount, or a sync to local disk). Corpus files are written
atomically, so a reader sees old bytes or new. Publish the index immutable, swap it atomically, and
read it with `IETF_LLM_INDEX_IMMUTABLE=1` on a read-only mount (see [Storage](storage.md)). The
replica picks the change up without a restart. The cloud store backend does the publish and
visibility steps for you, fleet-wide.

When the embedding endpoint is down, `search_corpus`, `read_topic`, `search_groups` and
`which_corpus` return an error and `find_efforts` falls back to keyword ranking. Every other tool
keeps working; `grep_corpus` reads files rather than the index, so exact-string search survives.

## In-session gather

`start_gather` and `gather_status` let a client gather a corpus without a shell, and `stop_gather`
(with the token `start_gather` returned) cancels one. `gather_status` reports `queued`, `running`
(stage and elapsed time), `done`, `failed`, `cancelled` or `interrupted` (the gatherer ended early).
Status lives in `<corpus>/gather-status.json` and, on the cloud backend, in the control plane, so any
replica can report it. The live Datatracker lookups (`meeting_schedule`, `draft_status`) share the
switch.

These tools write and reach the network, the one exception to the server's read-only, offline
contract. They default on for stdio and off for HTTP; `IETF_LLM_ENABLE_GATHER` overrides either way,
and `IETF_LLM_INDEX_IMMUTABLE=1` turns the default off. On the local backend a gathered corpus is
durable only on the box that wrote it. On the cloud backend a gather publishes a new version under a
cross-host lease, so enabling it on a fleet is safe. On a torch-free image use a remote
`openai-embed/...` model.

**Concurrency.** `IETF_LLM_GATHER_MAX_INFLIGHT` caps running gathers per host and, on the cloud
backend, fleet-wide. Further gathers queue up to `IETF_LLM_GATHER_QUEUE_MAX`, past which
`start_gather` is refused. A request for a corpus already gathering anywhere reports *already
running*; one with no free slot reports `queued`. The client polls `gather_status` and does not
retry. A queued or running record whose gatherer died becomes `interrupted`: on the cloud backend
when its lease lapses, on the local backend when the writer's pid is gone or the record is over an
hour old.

**CLI gathers** (`ietf-llm <corpus>`, `--all`) share the lifecycle. A mistyped name, or a corpus
gathered within the freshness window (`IETF_LLM_GATHER_MIN_INTERVAL`), exits before taking a lease,
slot or status record; `--force` overrides the window. On the cloud backend a CLI gather takes the
corpus lease (exiting non-zero if one is held) and shows in `gather_status`. On the local backend a
CLI gather and an in-session gather in separate processes do not exclude each other.

`IETF_LLM_HTTP_MAX_DATATRACKER` and `IETF_LLM_HTTP_MAX_PER_HOST` cap gather HTTP requests in flight
per host across all gathers in the process; lowering them is gentler on upstreams and slower.

**Scale-to-zero platforms.** A gather runs on the replica that accepted it and is durable only at
publish. A platform that sleeps or evicts an idle instance (Cloudflare Containers' `sleepAfter` is
one) drops the unpublished work, and the gather becomes `interrupted`. A background gather does not
count as activity and the container cannot keep itself awake, so the deployment has to: raise the
idle window beyond the longest gather, or renew it from the controlling runtime while `/health`
shows `gathers_inflight` above 0, routing the renewal to the replica that owns the gather. A deploy
or crash still loses unpublished work.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Exits 1 at boot | A [boot validation](#boot-time-config-validation) failure | The log names the setting. |
| `/health` is `503`, `index_probe: failed` | A WAL `embeddings.db` on a read-only mount, or a truncated index | Set `IETF_LLM_INDEX_IMMUTABLE=1` on a read-only mount; otherwise re-publish the index. The index must be on a local POSIX filesystem, never NFS or FUSE ([Storage](storage.md)). |
| `/health` is `200`, `index_probe: no-corpora` | No corpus gathered yet | Expected; gather on the write side. |
| Search tools error, others work | The embedding endpoint is unreachable | Check `IETF_LLM_EMBED_BASE_URL` and the token. |
| Replica serves stale content | The write side did not publish, or the resolve cache is stale | Compare `corpora.oldest` in `/health`. On cloud, lower `IETF_LLM_RESOLVE_TTL` and confirm the gather published; on local, confirm the sync or mount updated. |
| `grep_corpus` replies `INCOMPLETE SCAN` | A regex outran its 20 s budget | Narrow with `file_pattern` or search a literal phrase. |
| `start_gather` refused (queue full) | Backlog reached `IETF_LLM_GATHER_QUEUE_MAX` | Wait, or raise the cap and `IETF_LLM_GATHER_MAX_INFLIGHT` if upstreams tolerate it. |
| `421 Misdirected Request` | `IETF_LLM_MCP_ALLOWED_HOSTS` is set and the `Host` is not listed | Add the host, or unset the variable if a proxy validates `Host`. |
| Cloud store errors about credentials | Missing or invalid AWS credentials, or an IAM policy lacking an operation | Check `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or the instance role) and the policy in [Storage](storage.md). |
| Concurrent gathers clobber each other on cloud | The bucket ignores conditional writes (`If-Match` / `If-None-Match`) | The control plane needs them; use an object store that supports them. |
