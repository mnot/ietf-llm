# ietf-llm documentation

Guides beyond the [main README](../README.md), which covers installation and the common local setup.

## Using it

- **[Gathering a corpus](gathering.md)** — the shared first step: gather an IETF corpus into the
  local cache.
- **[Command & gather reference](reference.md)** — every `ietf-llm` command and gather flag.
- **[Search from the CLI](search-cli.md)** — semantic search over the cache with `ietf-llm-search`.
- **[Running the MCP server locally](mcp-local.md)** — stdio, one subprocess per client; per-client
  setup for Claude, Codex, Cursor, Gemini, opencode, Zed.
- **[NotebookLM export](notebooklm.md)** — export to a directory, or push to NotebookLM Enterprise.
- **[Shell completion](shell-completion.md)** — tab-complete commands, flags, and cached corpus
  names.

## Configuring & deploying

- **[Model backends](models.md)** — the embedding model and the summariser, on-device or remote
  (OpenAI-compatible), and how the global model settings resolve.
- **[Storage & locations](storage.md)** — relocate the cache, config, and index directories (e.g.
  the hot index onto tmpfs).
- **[Running the MCP server over HTTP](mcp-server.md)** — serve many concurrent clients from one
  process; health checks, logging, secrets.

## Contributing

- **[Architecture](architecture.md)** — the shape of the system, where state lives, and the
  non-obvious design decisions.
