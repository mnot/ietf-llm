# Running the MCP server over HTTP

**This document is for:** running `ietf-llm-mcp` as a shared service — one process
serving many clients over HTTP, rather than a per-user stdio subprocess. — Back to the
[docs index](README.md).

For local use, the MCP server speaks stdio and needs none of this; see
[Running the MCP server locally](mcp-local.md).

A hosted deployment is the stdio server with three things configured around it:

1. **Transport** — serve Streamable HTTP instead of stdio (below).
2. **[Embeddings](embedding.md)** — point at a remote endpoint so the image carries no torch
   and the read path makes no local model load.
3. **[Storage](storage.md)** — relocate the corpus and index directories (e.g. the index onto
   tmpfs).

The read tools touch no network and never write: every query opens its own read-only SQLite
connection, and the index is read as-is (gather is the only writer). So multiple clients — and a
concurrent re-gather — are safe against one corpus.

## Installing

```bash
pipx install ietf-llm
```

A base install is torch-free: the server embeds against a remote endpoint
([Embeddings](embedding.md)), not an on-device model. Corpora are gathered separately on the write
side (where `IETF_LLM_CACHE_DIR` is writable); the server only reads.

## Transport

Set `IETF_LLM_MCP_TRANSPORT=http`. The server will serve MCP as Streamable HTTP, so a fronting proxy
can be near-transparent.

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_MCP_TRANSPORT` | `stdio` or `http` | `stdio` |
| `IETF_LLM_MCP_HOST` | bind address | `127.0.0.1` |
| `IETF_LLM_MCP_PORT` | bind port | `8000` |

The MCP endpoint is served at `/mcp`.

```bash
IETF_LLM_MCP_TRANSPORT=http IETF_LLM_MCP_HOST=0.0.0.0 ietf-llm-mcp
```

## Health

`GET /health` is a readiness probe for a load balancer or orchestrator. It returns `200` once the
index directory is mounted and usable, `503` otherwise, with a small JSON body. It makes **no**
upstream call — a slow or unreachable embedding endpoint won't flap readiness — so it reports
"configured and ready to serve", not "the backend answered".

## Logging

Set `IETF_LLM_LOG_FORMAT=json` for one-line structured log records (`ts` / `level` / `msg`) that a
log collector can ingest; the default is human-readable text. Logs go to stderr (stdout is reserved
for the stdio protocol; container runtimes capture stderr). Log messages carry no secrets.
`IETF_LLM_DEBUG_LOG=1` additionally records per-request timing telemetry.

| Variable | Purpose | Default |
|---|---|---|
| `IETF_LLM_LOG_FORMAT` | `text` or `json` | `text` |
| `IETF_LLM_TOOL_TIMEOUT` | per-tool-call deadline, seconds (`0` disables) | `120` |
| `IETF_LLM_DEBUG_LOG` | per-request timing telemetry | off |

## Secrets and access

- **Secrets come from the environment only** — the embedding token and any other credential are read
  from the environment and never written to disk or to the per-corpus config.
- **Access control is yours to put in front.** The server has no built-in authn/z; the corpus is the
  public IETF record. Bind it to an internal interface and front it with whatever your platform uses
  for access and TLS.

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
export IETF_LLM_MCP_HOST=0.0.0.0
export IETF_LLM_LOG_FORMAT=json

ietf-llm-mcp
```

[Gathering corpora](gathering.md) is still a separate, write-side step (`ietf-llm <name>`), run
wherever you can write to `IETF_LLM_CACHE_DIR`; the server only ever reads.
