# Storage & locations

**This document is for:** relocating where ietf-llm keeps its files — the corpus cache, the config,
and the per-corpus search index — for example onto a faster volume or a mounted directory in a
container. — Back to the [docs index](README.md).

By default everything lives under the usual home directories:

```
~/.cache/ietf-llm/<name>/         the gathered corpus + its embeddings.db
~/.config/ietf-llm/             config (global config.json, per-corpus dirs)
```

The cache is the single source of truth; the corpus tree is self-contained and relocatable (chunk
paths are relative to the cache root). Each location can be pointed elsewhere with an environment
variable:

| Variable | What | Default |
|---|---|---|
| `IETF_LLM_CACHE_DIR` | corpus root (the gathered files) | `~/.cache/ietf-llm` |
| `IETF_LLM_CONFIG_DIR` | config root | `~/.config/ietf-llm` |
| `IETF_LLM_INDEX_DIR` | per-corpus `embeddings.db` files | the cache root |

An unset (or blank) variable falls back to the default, so the local CLI is unaffected.

## Separating the index from the corpus

`IETF_LLM_INDEX_DIR` exists because the index has a different access profile from the rest of the
corpus: search reads the whole of a corpus's `embeddings.db` on every query, where the corpus
files are read occasionally. Pointing the index at fast or RAM-backed storage (tmpfs) while the
corpus lives elsewhere can be worthwhile for a busy server:

```bash
export IETF_LLM_CACHE_DIR=/data/ietf-llm        # corpus, on a normal volume
export IETF_LLM_INDEX_DIR=/dev/shm/ietf-llm      # embeddings.db, on tmpfs
```

The layout under each root is the same as the defaults (`<root>/<name>/…`), so a directory can be
moved by setting the variable and relocating its contents — no re-gather required.

## Notes

- `IETF_LLM_CACHE_DIR` only needs to *exist* and be readable for a read-only consumer (the MCP
  server, `ietf-llm-search`); gather needs it writable.
- These pair naturally with [running the MCP server over HTTP](mcp-server.md), where the
  corpus is read-only and the index can sit on tmpfs for speed.
