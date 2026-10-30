# ietf-llm documentation

For an introduction to ietf-llm, see the [main README](../README.md).

## Using ietf-llm

- **[Gathering a corpus](gathering.md)** — the first step for every workflow: gather an IETF corpus
  into the local cache.
- **[Running the MCP server locally](mcp-local.md)** — over stdio, for local agents. *Start here*
- **[Running the MCP server over HTTP](mcp-server.md)** — serve many concurrent clients from one
  process.
- **[Search from the CLI](search-cli.md)** — semantic search over the cache with `ietf-llm-search`.
- **[NotebookLM export](notebooklm.md)** — export to a directory, or push to NotebookLM Enterprise.

## Reference and configuration

- **[Command & gather reference](reference.md)** — every `ietf-llm` command and gather flag.
- **[Shell completion](shell-completion.md)** — tab-complete commands, flags, and cached corpus
  names.
- **[Model backends](models.md)** — the embedding model and the summariser, on-device or remote,
  and how the global model settings resolve.
- **[Storage & locations](storage.md)** — the cache, config and index directories, and the cloud
  object-store backend.
- **[Seed store](seed-store.md)** — the public snapshot a gather seeds a corpus from, and how to
  publish one.

## Contributing

- **[Architecture](architecture.md)** — the overview, corpus kinds, cache and config layout, data
  flow, and a map of the packages.
