# Running the MCP server locally

**This document is for:** running `ietf-llm-mcp` over **stdio** on your own machine for use with  Claude Desktop, Claude Code, Codex, Cursor, Gemini, opencode, Zed, and other MCP-capable agents. —
Back to the [docs index](README.md).

To serve many clients from one shared process instead, see
[Running the MCP server over HTTP](mcp-server.md).

## 1. Install the package

[pipx](https://pipx.pypa.io/stable/) is recommended (see [its installation instructions](https://pipx.pypa.io/stable/how-to/install-pipx/) if this fails):

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The `[local-embeddings]` option installs an embedding backend for search (necessary for a local server).

**Behind a corporate firewall** with TLS interception? If you encounter errors, you may need the
`certs` extra:

```bash
pipx uninstall ietf-llm
pipx install 'ietf-llm[local-embeddings,certs]'
```


## 2. Register the server with your client

Register `ietf-llm-mcp` via your client's config file per below. Clients are listed alphabetically;
the snippets are correct as of writing, but if your client has changed since, its own MCP docs are
authoritative.

**Gotcha (all clients):** the binary may be on your shell `PATH` but not on the `PATH` inherited by
your client. Use the absolute path (`which ietf-llm-mcp`) if the client can't find the command.

The server hands its routing and norms guidance to every client through the MCP `instructions`
field, so any compliant client picks up the routing rules automatically — no skill required. You
can optionally install the two IETF *norms* skills locally as a convenience (see below), but
routing itself always comes from the server.

### Claude Code

```bash
claude mcp add ietf-llm -- ietf-llm-mcp
```

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

## 3. Gather

Many of the tools require [gather a corpus of materials](gathering.md) from IETF servers. This can be done on the command line with `ietf-llm` (see the link above), or you can just ask the
assistant to.

A local stdio server registers the `start_gather` / `gather_status` tools by default, letting the
assistant gather a corpus in-session instead of you running `ietf-llm <name>` in a shell.

To turn this **off** — for instance if this server points at a read-only-mounted cache — set
`IETF_LLM_ENABLE_GATHER=0` in its `env`:

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "ietf-llm-mcp",
      "env": { "IETF_LLM_ENABLE_GATHER": "0" }
    }
  }
}
```


## 4. Installing the norm skills (optional)

You can install the two IETF **norms** skills — `ietf-interpreting` and `ietf-contributing` (the
read- and write-side norms the server also serves via `read_ietf_interpretation_norms` /
`read_ietf_participation_norms`) — into every supported agent harness:

```bash
ietf-llm --install-skills
```

This is a convenience; it installs the same two norm skills you can install yourself from
[mnot/ietf-skill](https://github.com/mnot/ietf-skill) (their canonical home). Re-run after
upgrading to pick up a newer pin.


## Tuning the MCP server

Each tool call has a server-side deadline so a stuck call fails fast with a clear message rather
than hanging until the client's timeout. It defaults to 120 seconds; override (or disable, with
`0`) by setting `IETF_LLM_TOOL_TIMEOUT` in the server's environment — e.g. add `"env":
{"IETF_LLM_TOOL_TIMEOUT": "180"}` to a client's JSON config.
