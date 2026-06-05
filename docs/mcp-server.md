# Running the MCP server over HTTP

**This document is for:** running `ietf-llm-mcp` as a shared service — one process
serving many clients over HTTP, rather than a per-user stdio subprocess. — Back to the
[docs index](README.md).

For local use, the MCP server speaks stdio and needs none of this; see
[Running the MCP server locally](mcp-local.md).

A hosted deployment is the stdio server with three things configured around it:

1. **Transport** — serve Streamable HTTP instead of stdio (below).
2. **[Embeddings](models.md#embeddings)** — point at a remote endpoint so the image carries no torch
   and the read path makes no local model load.
3. **[Storage](storage.md)** — relocate the corpus and index directories (e.g. the index onto
   tmpfs).

The read tools touch no network and never write: every query opens its own read-only SQLite
connection, and the index is read as-is (gather is the only writer). So multiple clients — and a
concurrent re-gather — are safe against one corpus.

## Deployment contract

Read this before exposing the HTTP server to anything but localhost.

`ietf-llm-mcp` is a read-only query surface over the *public* IETF record. It is **not designed to
sit on the open Internet.** It assumes a trust boundary you control — run it on an internal
interface (the default bind is `127.0.0.1`) and put your own proxy in front of anything wider.

**The server provides no identity, rate limiting, or cost control.** Concretely:

- **No authn/z.** There is no login, no API key, no per-client identity. `IETF_LLM_MCP_HOST` defaults
  to `127.0.0.1`; widen it (`0.0.0.0`) only behind a proxy. Any identity is the proxy's job.
- **No rate limiting.** Nothing caps request rate or concurrency beyond the per-call tool deadline
  (`IETF_LLM_TOOL_TIMEOUT`). One client can saturate CPU. Rate and concurrency limits are the
  proxy's job.
- **No quota or budget cap.** `search_corpus` and `read_topic` each embed their query, which for a
  remote backend is a metered, paid `/v1/embeddings` call per request — with no quota, no budget
  ceiling, no circuit breaker, and no query-embedding cache. A busy or hostile client runs up the
  embedding bill. Quota is the proxy's (or the upstream account's) job.

**If you front it with a proxy, that proxy owns identity, rate limiting, and quota** — the server
will not do any of it, now or by configuration.

**Threat model: availability, cost, and abuse — not confidentiality.** The read path serves only
the public IETF record (mailing lists, drafts, RFCs, minutes — all already public at ietf.org).
There is nothing secret to leak, so the risks worth sizing controls against are denial of service,
the embedding bill, and abuse of compute — not data exposure. The secrets that *do* exist (the
embedding token, any other credential) are read from the environment only and are never written to
disk, to the per-corpus config, or back to a client.

The one exception to "read-only" is the opt-in [in-session gather](#in-session-gather-opt-in) below,
which writes and reaches the network; leave it off for an exposed replica.

## Boot-time config validation

The HTTP serve path validates its configuration **before binding**, so a contradictory or
under-provisioned config fails fast at boot rather than minutes into a gather or on the first
`search_corpus`. It is not transport-gated — HTTP + in-session gather is a supported trusted-box
shape — it validates cross-knob *consistency*:

- **Hard refuse** (logs the reason, exits 1) for configs that cannot work: `IETF_LLM_ENABLE_GATHER`
  together with `IETF_LLM_INDEX_IMMUTABLE` (gather must write the index the mount marks read-only);
  gather with a local torch-backed embed model on a torch-free image and no `--no-embed` (the embed
  step would crash mid-pipeline); a remote `openai-embed/...` model with no `IETF_LLM_EMBED_BASE_URL`
  (the read path would fail at request time); `IETF_LLM_STORE_BACKEND=cloud` with the cloud store
  under-configured (missing `IETF_LLM_CONTROL_DB` / `IETF_LLM_BLOB_DIR` / `IETF_LLM_SCRATCH_DIR`), or
  an unrecognised backend name.
- **Warn but never block** when the bind host is non-loopback — the no-auth / no-rate-limit posture
  above is the operator's risk to own; the warning is louder when gather is also on.
- **Always** log a one-line posture banner (transport, bind, gather on/off, embed backend, embed
  model, index dir, immutable, store backend), honouring `IETF_LLM_LOG_FORMAT=json`, so the logs
  answer "what is this process actually doing" — dovetailing with the version / freshness preamble
  below.

## Installing

```bash
pipx install ietf-llm
```

A base install is torch-free: the server embeds against a remote endpoint
([Embeddings](models.md#embeddings)), not an on-device model. Corpora are gathered separately on
the write side (where `IETF_LLM_CACHE_DIR` is writable); the server only reads.

For an S3 / R2 / MinIO blob store (the cloud store backend), add the `s3` extra:
`pipx install 'ietf-llm[s3]'`. See [Storage](storage.md#corpus-store-backend-local-vs-cloud).

## Transport

Set `IETF_LLM_MCP_TRANSPORT=http`. The server will serve MCP as Streamable HTTP, so a fronting proxy
can be near-transparent.

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_MCP_TRANSPORT` | `stdio` or `http` | `stdio` |
| `IETF_LLM_MCP_HOST` | bind address | `127.0.0.1` |
| `IETF_LLM_MCP_PORT` | bind port | `8000` |
| `IETF_LLM_MCP_STATELESS` | stateless sessions (`0`/`false` for stateful) | `1` (on) |
| `IETF_LLM_MCP_ALLOWED_HOSTS` | comma-separated `Host` allow-list (enables DNS-rebinding protection) | unset (off) |
| `IETF_LLM_MCP_ALLOWED_ORIGINS` | comma-separated `Origin` allow-list (browser callers) | unset (any) |

The MCP endpoint is served at `/mcp`.

```bash
IETF_LLM_MCP_TRANSPORT=http IETF_LLM_MCP_HOST=0.0.0.0 ietf-llm-mcp
```

### Stateless sessions

The HTTP transport runs **stateless by default**: the server keeps no
`Mcp-Session-Id` state between requests, so any replica behind a load balancer
can answer any request with no session affinity — the right shape for this
read-mostly server. Set `IETF_LLM_MCP_STATELESS=0` (or `false`/`no`/`off`) to
restore stateful per-client sessions. The setting is ignored by the stdio
transport. The boot posture banner reports the effective `stateless` value.

### Host / Origin allow-list

By default the server does **no** `Host`-header validation: it assumes a trust
boundary (a proxy or firewall) in front, which is the intended production shape.
If you instead expose the server directly, set `IETF_LLM_MCP_ALLOWED_HOSTS` to the
exact public host(s) you serve under — this turns on DNS-rebinding protection, and
any request whose `Host` header is not on the list is rejected with `421 Misdirected
Request`. Each entry is an exact `host` or `host:port`, or a `host:*` wildcard that
matches the host on any port (e.g. `mcp.example.org,localhost:*`).
`IETF_LLM_MCP_ALLOWED_ORIGINS` similarly restricts the `Origin` header for browser
callers; leave it unset to accept any origin. The boot posture banner reports the
effective `host_allowlist` (`off` when unset).

## Health

`GET /health` is a readiness probe for a load balancer or orchestrator. It returns `200` once the
index directory is mounted **and a corpus index actually opens** — the probe runs a trivial read
against one `embeddings.db`, so an index that is present but unservable (e.g. a WAL database on a
read-only mount without `IETF_LLM_INDEX_IMMUTABLE`, or a truncated file) fails the probe instead of
passing it. A server with no corpora gathered yet is still ready. `503` otherwise, with a small JSON
body (an `index_probe` field reports `ok` / `no-corpora` / `failed`). It makes **no** upstream call
— a slow or unreachable embedding endpoint won't flap readiness — so it reports "configured and
ready to serve", not "the backend answered".

The JSON body also carries two operator-facing fields that don't gate readiness. `version` is the
running package version, for correlating behaviour across a rolling deploy. `corpora` is a bounded
freshness summary read from the per-corpus `last-gathered` sentinels (no upstream call): `count`
(all cached corpora), `tracked` (those carrying a sentinel — caches predating freshness tracking
have none), and `oldest` / `newest`, each `{corpus, last_gathered, age_seconds}` (or `null` when
nothing is tracked). `oldest` is the staleness floor — a replica can be perfectly ready while
serving a corpus that went stale days ago, and this is how an operator sees that at a glance. It is
deliberately a summary, not a per-corpus row, so a box serving many corpora keeps a small payload;
the per-corpus breakdown belongs to a future `/metrics` scrape.

## Cache freshness and degraded mode

**The serve process never writes the cache.** `gather` is the only writer (`ietf-llm <name>`), run
out-of-band on a write node where `IETF_LLM_CACHE_DIR` is writable; a serving replica only ever
reads. So fresh data reaches a read replica out-of-band, in three steps:

1. Gather on the write side, producing the corpus tree and its `embeddings.db`.
2. Publish those to the storage the replica reads — a shared mount, or a sync to the replica's local
   disk. Corpus writes are atomic (temp + rename), so a reader sees the old bytes or the new, never a
   torn file. The index is a SQLite database: publish it immutable and swap it atomically, then read
   it with `IETF_LLM_INDEX_IMMUTABLE=1` on a read-only mount (see [Storage](storage.md)).
3. The replica picks it up with no restart — every tool opens a fresh read-only connection per call.

The **cloud store backend** (`IETF_LLM_STORE_BACKEND=cloud`, see
[Storage](storage.md#corpus-store-backend-local-vs-cloud)) does steps 2–3 for you: a publish is
visible fleet-wide with no separate sync step and no torn read, so a read replica needs no manual
publish/sync. It is also what makes the [in-session gather](#in-session-gather-opt-in) durable and
fleet-coherent rather than a single-box convenience. Mechanics are in
[architecture.md](architecture.md).

**Degraded mode when the embedding upstream is down.** Only two tools embed their query, and they are
the only ones that fail if the remote `/v1/embeddings` endpoint is unreachable:

- **Fail:** `search_corpus` and `read_topic` — both embed the query to do semantic search.
- **Keep working:** every deterministic tool — `overview`, `read_digest`, `list_corpora`,
  `list_files`, `list_labels`, `find_citations`, `find_replies`, `tally_positions`, `rfc_search` /
  `get_rfc`, `read_file_section`, and `get_chunk_text` / `get_chunks_batch`. (`fetch_by_url` also
  keeps working; it fetches a URL rather than embedding, so it's independent of the embedding backend
  but not of network egress.)

`GET /health` makes no upstream call (see above), so a down embedding endpoint degrades search
without flapping readiness.

## Logging

Set `IETF_LLM_LOG_FORMAT=json` for one-line structured log records (`ts` / `level` / `msg`) that a
log collector can ingest; the default is human-readable text. Logs go to stderr (stdout is reserved
for the stdio protocol; container runtimes capture stderr). Log messages carry no secrets.
`IETF_LLM_DEBUG_LOG=1` additionally records per-request timing telemetry.

When serving over HTTP, the server emits a one-line startup preamble at this same log level —
version, bind address, and the `corpora` freshness floor — mirroring `/health`, so a deploy log
shows which build a replica came up on and how stale its caches were at boot.

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_LOG_FORMAT` | `text` or `json` | `text` |
| `IETF_LLM_TOOL_TIMEOUT` | per-tool-call deadline, seconds (`0` disables) | `120` |
| `IETF_LLM_DEBUG_LOG` | per-request timing telemetry | off |
| `IETF_LLM_ENABLE_GATHER` | register the in-session gather tools (writes + network) | off |

## In-session gather (opt-in)

The server is read-only by default. Setting `IETF_LLM_ENABLE_GATHER=1`
registers two extra tools so a client can gather a new corpus without
dropping to a shell:

- `start_gather(corpus, [mailing_list], [draft], [github], [author], [new_drafts], …)`
  — enqueues a gather and returns immediately. The corpus shape (group / list /
  custom / synthetic) is inferred from the arguments.
- `gather_status([corpus])` — reports `queued`, `running` (with the current
  stage, e.g. `stage 7/17 (github issues)`, and elapsed time), `done`, `failed`
  (with the error), or `interrupted` (the gatherer ended before completion).
  Status is persisted to `<corpus>/gather-status.json` and, on the cloud
  backend, to the control plane so any replica can report it.

### Gather concurrency

Gathers are serialised to stay polite to shared upstreams (datatracker,
mailarchive, GitHub), under two nested caps:

- **Per host:** a single worker runs **one gather at a time**; further requests
  queue (FIFO). `IETF_LLM_GATHER_QUEUE_MAX` bounds the backlog (default `16`);
  past it, `start_gather` is refused rather than piling up unbounded work.
- **Fleet-wide:** at most `IETF_LLM_GATHER_MAX_INFLIGHT` gathers run across the
  whole deployment at once (default `1` — one gather anywhere at a time). The
  cap is a global slot in the control plane, so it only applies on the **cloud
  backend**; on the local backend the per-host worker is the only bound.

A request for a corpus already in flight (here or on another host) reports
*already running*; a request for a different corpus when no slot is free is
accepted as `queued`. Either way the client polls `gather_status` — it does not
retry. The per-corpus lease is taken at enqueue, so the same corpus is never
gathered twice across the fleet, and a `queued`/`running` record whose gatherer
died is relabelled `interrupted` once its lease lapses.

> The CLI (`ietf-llm <corpus>`) does **not** yet participate in the fleet-wide
> slot, so a cron/shell gather runs alongside the cap rather than counting
> toward it. Bringing the CLI under the same slot is a planned follow-up.

This is the one break from the read-only / no-network contract — leave it
**off** for a shared HTTP replica or a read-only-mounted cache *on the local
backend*, where a gathered corpus is only durable on the box that wrote it. On
the **cloud backend** it is a first-class shape: the gather publishes a new
immutable version through the store (atomic pointer flip, visible to every
replica) under a cross-host lease, so enabling it on a replicated fleet is safe —
see [Cache freshness](#cache-freshness-and-degraded-mode). If you do enable it on
the torch-free serve image, use a remote `openai-embed/...` embedding model so
the gather's index build pulls no torch.

## A minimal deployment

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
export IETF_LLM_MCP_HOST=0.0.0.0   # bind wide only behind a proxy (see Deployment contract)
export IETF_LLM_LOG_FORMAT=json

ietf-llm-mcp
```

[Gathering corpora](gathering.md) is normally a separate, write-side step (`ietf-llm <name>`), run
wherever you can write to `IETF_LLM_CACHE_DIR`; the server only ever reads — unless you opt into the
in-session gather tools above.
