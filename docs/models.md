# Model backends

**This document is for:** configuring ietf-llm's two model backends — the embedding model behind
semantic search and the optional summariser — on-device or against a remote OpenAI-compatible
endpoint, and how the global model settings resolve. — Back to the [docs index](README.md).

ietf-llm has two model touch-points, and both are configured the same way — on-device, or against a
remote OpenAI-compatible endpoint:

- The **embedding model** builds and queries the semantic-search index. Required for search.
- The **summariser** writes short summaries into the issues and threads digests at gather time.
  Optional (`--summarize`).

## Embeddings

Both `ietf-llm-search` and the MCP `search_corpus` tool run over a per-corpus vector index built
during gather. The model that builds the index is recorded in it, so search always queries with
the same backend.

### On-device (default)

`BAAI/bge-small-en-v1.5` runs locally via sentence-transformers — ~130 MB, ~33M params,
MPS-accelerated on Apple Silicon, no API key. It requires installing the `local-embeddings` extra
(torch is not in the base install):

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The weights download from Hugging Face on first use and are cached; subsequent gathers reuse them.

### Remote embedding endpoint

To embed against a hosted `/v1/embeddings` service instead — so the install (and any serving
container) needs no torch — set the model id to an `openai-embed/<model-id>` prefix and point at the
endpoint with the environment:

```bash
export IETF_LLM_EMBED_MODEL=openai-embed/bge-small-en-v1.5
export IETF_LLM_EMBED_BASE_URL=https://your-endpoint/v1
export IETF_LLM_EMBED_TOKEN=...          # kept in the environment only
ietf-llm httpbis
```

The backend is provider-neutral — anything that speaks the OpenAI embeddings contract (a hosted
service, a gateway, a self-hosted vLLM / TEI) works; the URL and token are all that change.

| Variable | Purpose |
|---|---|
| `IETF_LLM_EMBED_BASE_URL` | endpoint base URL (e.g. `https://host/v1`) |
| `IETF_LLM_EMBED_TOKEN` | bearer token (sent as `Authorization: Bearer`) |
| `IETF_LLM_EMBED_HEADERS` | extra request headers as a JSON object (e.g. an API-gateway header alongside the token) |
| `IETF_LLM_EMBED_BATCH` | inputs per request (default 96) |
| `IETF_LLM_EMBED_TIMEOUT` | per-request timeout, seconds (default 10) |
| `IETF_LLM_EMBED_RETRIES` | retries on `429` / `5xx`, with backoff (default 3) |
| `IETF_LLM_EMBED_CONCURRENCY` | files embedded in parallel during a gather (default 8, floor 1 = serial) |

The token comes from the environment only and is never written to disk. Bulk ingest batches to
`IETF_LLM_EMBED_BATCH` inputs per request and backs off on rate-limit / server errors. Because each
request is a network round-trip, a gather embeds up to `IETF_LLM_EMBED_CONCURRENCY` files at once
(the index write stays single-threaded); raise it for more throughput against a generous endpoint,
lower it (or `1`) if the endpoint rate-limits. The embedding session's connection pool is sized to
this value (never below the default 10), so raising it scales the keep-alive connections with the
fan-out instead of exhausting the pool. The on-device model ignores this — it embeds serially.

### One index, one backend

The model id is stored in the index, and search reads it back to resolve the same backend
automatically. Vectors are **not** portable across backends — even the "same" model isn't
bit-identical across inference runtimes — so:

- The on-device and remote id strings never collide (`sentence-transformers/…` vs `openai-embed/…`),
  so an index built one way is never queried the other way by accident.
- A change of model **or** vector dimension is detected and forces a rebuild on the next gather,
  rather than mixing incompatible vectors.

If you switch a corpus from local to remote (or vice versa), the next `ietf-llm <name>` re-embeds
it.

## Summarisation

`--summarize` writes a one-line LLM summary into the issues and threads digests (off by default). By
default it runs through Simon Willison's [`llm`](https://llm.datasette.io/) library, so you can use
any provider `llm` knows about — OpenAI is built in; Anthropic, Gemini, and others need their
`llm-<provider>` plugin — configured with `llm`'s own settings on the host that runs gather.

### Remote summariser endpoint

For parity with embeddings — so a deployment configures both touch-points identically — set the
summariser model to an `openai-summarize/<model-id>` prefix and point at a chat-completions endpoint
with the environment:

```bash
export IETF_LLM_SUMMARIZE_MODEL=openai-summarize/gpt-4o-mini
export IETF_LLM_SUMMARIZE_BASE_URL=https://your-endpoint/v1
export IETF_LLM_SUMMARIZE_TOKEN=...        # kept in the environment only
ietf-llm httpbis --summarize
```

Same provider-neutral contract as embeddings: anything serving OpenAI-compatible
`/v1/chat/completions` works (a hosted service, a gateway, a self-hosted vLLM). The id after the
prefix is whatever the endpoint calls the model — e.g. `@cf/meta/llama-3.1-8b-instruct` on a
Cloudflare Workers AI gateway. Any model id **without** the prefix still goes through `llm`, so
non-OpenAI providers remain available that way.

| Variable | Purpose |
|---|---|
| `IETF_LLM_SUMMARIZE_BASE_URL` | endpoint base URL (e.g. `https://host/v1`) |
| `IETF_LLM_SUMMARIZE_TOKEN` | bearer token (sent as `Authorization: Bearer`) |
| `IETF_LLM_SUMMARIZE_HEADERS` | extra request headers as a JSON object (e.g. an API-gateway header alongside the token) |
| `IETF_LLM_SUMMARIZE_TIMEOUT` | per-request timeout, seconds (default 30) |
| `IETF_LLM_SUMMARIZE_RETRIES` | retries on `429` / `5xx`, with backoff (default 3) |

The token comes from the environment only and is never written to disk. Unlike the embedding model,
the summariser id is **not** recorded in any index — summaries are plain text in the cache — so
switching providers needs no rebuild; the next `--summarize` gather just uses the new backend.

## Global settings

The embedding model, embed on/off, and the summariser are properties of the tool — not of any one
corpus — so as of 0.8.0 they're configured **globally**, and resolve with this precedence:

```
environment  >  CLI flag  >  global config (~/.config/ietf-llm/config.json)  >  default
```

A non-default CLI flag is written through to the global config, so it sticks for every corpus. The
environment wins even over an explicit flag (so a container's injected configuration is
authoritative); when it overrides a flag you passed, a notice is logged.

| Setting | Flag | Environment | Default |
|---|---|---|---|
| Embedding model | `--embed-model` | `IETF_LLM_EMBED_MODEL` | `bge-small-en-v1.5`, on-device |
| Skip embedding | `--no-embed` | `IETF_LLM_NO_EMBED` | off |
| Summaries | `--summarize` | `IETF_LLM_SUMMARIZE` | off |
| Summariser model | `--summarize-model` | `IETF_LLM_SUMMARIZE_MODEL` | the `llm` default |

The `*_BASE_URL` / `*_TOKEN` / `*_HEADERS` endpoint variables for either backend (above) are read
from the environment at gather time and are **never** persisted — secrets stay in the environment,
not in the global config.

