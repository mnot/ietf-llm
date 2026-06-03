# Running the MCP server locally

**This document is for:** running `ietf-llm-mcp` on your own machine — one server subprocess per
client — for use with Claude, Codex, Cursor, Gemini, opencode, Zed, and other MCP-capable agents. —
Back to the [docs index](README.md).

`ietf-llm-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes
the local corpus to any MCP-capable agent. Set up once, gather each corpus you care about once,
then ask questions indefinitely. It speaks MCP over **stdio** — one server subprocess per client
— which is what the per-client setups below expect. (To serve many clients from one shared process
instead, see [Running the MCP server over HTTP](mcp-server.md).)

## Installing

```bash
pipx install 'ietf-llm[local-embeddings]'
```

Search needs an embedding backend: the on-device model via the `local-embeddings` extra, or a
[remote endpoint](embedding.md) (no torch). Then [gather a corpus](gathering.md)
before querying.

## Register with your client

Register the same command — `ietf-llm-mcp` — via your client's config file. Clients are listed
alphabetically; the snippets are correct as of writing, but if your client has changed since, its
own MCP docs are authoritative.

**Gotcha (all clients):** if `ietf-llm-mcp` was installed via `pipx`, the binary is on your shell
`PATH` but may not be on the `PATH` inherited by a GUI app launched from Finder / Spotlight /
Explorer. Use the absolute path (`which ietf-llm-mcp`) if the client can't find the command.

The server hands its usage guidance to every client through the MCP `instructions` field, so any
compliant client picks up the routing rules automatically. Claude Code can additionally install that
guidance as a local skill (see below); it's the same content either way.

### Claude Code

```bash
claude mcp add ietf-llm -- ietf-llm-mcp
```

Optionally install the bundled skill, which is the same guidance the server already exposes via MCP,
packaged for Claude Code:

```bash
ietf-llm --install-claude-skill
```

Re-run after upgrading the package to pick up improvements.

### Claude Desktop

Edit `claude_desktop_config.json` (create it if missing):

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "ietf-llm-mcp"
    }
  }
}
```

Quit and relaunch Claude Desktop — the config is only read at startup.

### Codex CLI (OpenAI)

`~/.codex/config.toml`:

```toml
[mcp_servers.ietf-llm]
command = "ietf-llm-mcp"
```

### Cursor

In-app MCP settings panel, or `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per-project):

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "ietf-llm-mcp"
    }
  }
}
```

### Gemini CLI

`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "ietf-llm-mcp"
    }
  }
}
```

### opencode

`~/.config/opencode/opencode.json` (or `opencode.json` in your project root):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ietf-llm": {
      "type": "local",
      "command": ["ietf-llm-mcp"],
      "enabled": true
    }
  }
}
```

### Zed

`~/.config/zed/settings.json`:

```json
{
  "context_servers": {
    "ietf-llm": {
      "command": {
        "path": "ietf-llm-mcp",
        "args": []
      },
      "settings": {}
    }
  }
}
```

## Tuning

Each tool call has a server-side deadline so a stuck call fails fast with a clear message rather
than hanging to the client's timeout. It defaults to 120 seconds; override (or disable, with `0`) by
setting `IETF_LLM_TOOL_TIMEOUT` in the server's environment — e.g. add
`"env": {"IETF_LLM_TOOL_TIMEOUT": "180"}` to a client's JSON config.
