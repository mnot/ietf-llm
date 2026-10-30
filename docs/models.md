# Model backends

**This document is for:** configuring the two model backends — the embedding model behind semantic
search and the optional summariser — on-device or against a remote OpenAI-compatible endpoint. Back
to the [docs index](README.md).

- The **embedding model** builds and queries the semantic-search index. Required for search.
- The **summariser** writes short summaries into the issues and threads digests at gather time.
  Optional (`--summarize`).

## Embeddings

`ietf-llm-search` and the MCP `search_corpus` tool run over a per-corpus vector index built during
gather. The index records the model that built it, and search queries with the same backend.

### On-device (default)

`BAAI/bge-small-en-v1.5` runs locally via sentence-transformers (about 130 MB, no API key). It needs
the `local-embeddings` extra, since torch is not in the base install:

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The weights download from Hugging Face on first use and are cached.

**Intel Macs** need Python 3.10–3.12, because PyTorch publishes no Intel-Mac wheels for 3.13 or
later (the install fails with "No matching distribution found for torch"). Install under an older
Python (`pipx install --python python3.12 'ietf-llm[local-embeddings]'`), or omit the extra and use
a remote endpoint.

**Device.** Embedding runs on the CPU, or on CUDA when it is available. MPS (the Apple Silicon GPU)
is not used by default, because PyTorch's MPS allocator uses several times the memory the CPU does
when indexing a corpus. Set `IETF_LLM_EMBED_DEVICE` to `cpu`, `mps` or `cuda` to override.

### Remote embedding endpoint

To embed against a hosted `/v1/embeddings` service, so the install (and any serving container)
needs no torch, set the model id to an `openai-embed/<model-id>` prefix and point at the endpoint
with the environment:

```bash
export IETF_LLM_EMBED_MODEL=openai-embed/bge-small-en-v1.5
export IETF_LLM_EMBED_BASE_URL=https://your-endpoint/v1
export IETF_LLM_EMBED_TOKEN=...
ietf-llm httpbis
```

Anything that speaks the OpenAI embeddings contract works: a hosted service, a gateway, or a
self-hosted vLLM or TEI.

| Variable | Purpose |
|---|---|
| `IETF_LLM_EMBED_BASE_URL` | endpoint base URL (e.g. `https://host/v1`) |
| `IETF_LLM_EMBED_TOKEN` | bearer token (sent as `Authorization: Bearer`) |
| `IETF_LLM_EMBED_HEADERS` | extra request headers as a JSON object (e.g. an API-gateway header alongside the token) |
| `IETF_LLM_EMBED_BATCH` | inputs per request (default 96) |
| `IETF_LLM_EMBED_TIMEOUT` | per-request timeout, seconds (default 10) |
| `IETF_LLM_EMBED_RETRIES` | retries on `429` / `5xx` for bulk ingest, with backoff (default 3) |
| `IETF_LLM_EMBED_QUERY_RETRIES` | retries for the single query-time embed behind a search, capped at `IETF_LLM_EMBED_RETRIES` (default 1) |
| `IETF_LLM_EMBED_CONCURRENCY` | files embedded in parallel during a gather (default 8; 1 is serial) |

The token is read from the environment only and never written to disk. A gather embeds up to
`IETF_LLM_EMBED_CONCURRENCY` files at once: raise it for throughput against a generous endpoint,
lower it if the endpoint rate-limits. The on-device model ignores it.

A `Retry-After` longer than 60 s (30 s for a query-time embed) is not waited out. During a gather
the embed stage then stops, files already embedded are kept, the gather reports the rate limit and
how long the provider asked for, and the remaining files are embedded on the next gather.

### One index, one backend

The index stores its model id, and search reads it back to pick the same backend. Switching a
corpus between local and remote, or changing the model or vector dimension, makes the next
`ietf-llm <name>` re-embed it, since vectors from different models are not compatible.

## Summarisation

`--summarize` writes a one-line LLM summary into the issues and threads digests (off by default). It
runs through Simon Willison's [`llm`](https://llm.datasette.io/) library, so any provider `llm`
knows about works: OpenAI is built in, and Anthropic, Gemini and others need their `llm-<provider>`
plugin, configured with `llm`'s own settings on the host that runs gather.

### Remote summariser endpoint

To use a chat-completions endpoint directly, set the summariser model to an
`openai-summarize/<model-id>` prefix and point at it with the environment:

```bash
export IETF_LLM_SUMMARIZE_MODEL=openai-summarize/gpt-4o-mini
export IETF_LLM_SUMMARIZE_BASE_URL=https://your-endpoint/v1
export IETF_LLM_SUMMARIZE_TOKEN=...
ietf-llm httpbis --summarize
```

Anything serving OpenAI-compatible `/v1/chat/completions` works. The id after the prefix is
whatever the endpoint calls the model. A model id without the prefix goes through `llm`.

| Variable | Purpose |
|---|---|
| `IETF_LLM_SUMMARIZE_BASE_URL` | endpoint base URL (e.g. `https://host/v1`) |
| `IETF_LLM_SUMMARIZE_TOKEN` | bearer token (sent as `Authorization: Bearer`) |
| `IETF_LLM_SUMMARIZE_HEADERS` | extra request headers as a JSON object |
| `IETF_LLM_SUMMARIZE_TIMEOUT` | per-request timeout, seconds (default 30) |
| `IETF_LLM_SUMMARIZE_RETRIES` | retries on `429` / `5xx`, with backoff (default 3) |

The token is read from the environment only and never written to disk. Summaries are plain text in
the cache, so switching summariser needs no rebuild; the next `--summarize` gather uses the new one.

## Global settings

The embedding model, whether to embed, and the summariser are properties of the tool rather than of
any one corpus, and resolve with this precedence:

```
environment  >  CLI flag  >  global config (~/.config/ietf-llm/config.json)  >  default
```

A CLI value is also written to the global config, so it applies to every corpus from then on. The
environment wins even over an explicit flag; when it does, a notice is logged.

| Setting | Flag | Environment | Default |
|---|---|---|---|
| Embedding model | `--embed-model` | `IETF_LLM_EMBED_MODEL` | `bge-small-en-v1.5`, on-device |
| Skip embedding | `--no-embed` (undo with `--embed`) | `IETF_LLM_NO_EMBED` | off |
| Summaries | `--summarize` | `IETF_LLM_SUMMARIZE` | off |
| Summariser model | `--summarize-model` | `IETF_LLM_SUMMARIZE_MODEL` | the `llm` default |

The `*_BASE_URL`, `*_TOKEN` and `*_HEADERS` variables are read from the environment at gather time
and are never persisted.
