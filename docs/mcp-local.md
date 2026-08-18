# Running the MCP server locally

**This document is for:** running `ietf-llm-mcp` over **stdio** on your own machine, for use with
MCP-capable agents. — Back to the [docs index](README.md).

To serve many clients from one shared process instead, see
[Running the MCP server over HTTP](mcp-server.md).

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [1. Install the package](#1-install-the-package)
- [2. Register the server with your client](#2-register-the-server-with-your-client)
- [3. Gather](#3-gather)
- [4. Installing the IETF skills (optional)](#4-installing-the-ietf-skills-optional)
- [Tuning the MCP server](#tuning-the-mcp-server)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## 1. Install the package

[pipx](https://pipx.pypa.io/stable/) is recommended (see [its installation instructions](https://pipx.pypa.io/stable/how-to/install-pipx/) if this fails):

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The `[local-embeddings]` option installs an embedding backend for search (necessary for a local server).

**Behind a corporate firewall** with TLS interception? Nothing extra to install — we verify
against your OS trust store, so a root your machine already trusts just works. If the proxy's
root is *not* installed there, point `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at its PEM.
(This replaces the old `certs` extra, which is gone.)


## 2. Register the server with your client

Register `ietf-llm-mcp` via your client's config file per below. Clients are listed alphabetically;
the snippets are correct as of writing, but if your client has changed since, its own MCP docs are
authoritative.

**Gotcha (all clients):** the binary may be on your shell `PATH` but not on the `PATH` inherited by
your client. Use the absolute path (`which ietf-llm-mcp`) if the client can't find the command.

The server hands its routing and norms guidance to every client through the MCP `instructions`
field, so any compliant client picks up the routing rules automatically — no skill required. You
can optionally install the bundled IETF skills locally as a convenience (see below), but routing
itself always comes from the server.

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

**NOTE**: Claude Desktop seems to have an MCP performance issue in the 'Chat' tab.
The 'Code' tab does not encounter this issue. Adding this to the MCP server
configuration may help:

```json
      "env": {
        "IETF_LLM_GATHER_MAX_WAIT": "0"
      }
```

**On WSL:** Claude Desktop is a native Windows process — it cannot exec a Linux
binary directly, so `pipx install`-ing `ietf-llm` inside a WSL distro is not
enough on its own. `claude_desktop_config.json` still lives on the Windows
side (`%APPDATA%\Claude\...`, as above); point its `"command"` at `wsl.exe`
instead of `ietf-llm-mcp` directly, and give it the absolute Linux path (find
it with `which ietf-llm-mcp` *inside* the distro):

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "wsl.exe",
      "args": ["-d", "<YourDistroName>", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"]
    }
  }
}
```

`-d <YourDistroName>` is optional if you only have one distro (`wsl -l -v` from
PowerShell lists them). Env vars in the config's `"env"` block are **not**
forwarded across the `wsl.exe` boundary — set them inside the distro instead
(e.g. in `~/.bashrc`, or hardcode them into a wrapper script you point `-e` at).

If instead you run **Claude Code inside WSL** (the common VS Code + WSL setup),
there is no split: the extension's server process is itself a WSL-side Linux
process, so it sees `ietf-llm-mcp` on its own `PATH` like any other Linux
install — `claude mcp add ietf-llm -- ietf-llm-mcp` (above) just works, no
`wsl.exe` wrapper needed.


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

**The RFC tools are the exception, and come first.** `search_rfc_index`,
`search_rfc_text`, `get_rfc_info` and `get_rfc_section` cover every published
RFC and belong to no corpus, so gathering does not bring them:

```
ietf-llm --init
```

That installs the RFC metadata mirror, the effort catalog and ~285 MB of RFC
full text from the seed store, then exits without gathering. A gather does the
same work afterwards as housekeeping, so this is only strictly needed if you
want the RFC tools before choosing a corpus — or on a machine that will never
gather at all.

Many of the tools require gather a corpus of materials from IETF servers. This can be done on the command line with `ietf-llm` (see [details](gathering.md)), or you can just ask the
LLM to do it.

A local stdio server registers the `start_gather` / `gather_status` tools by default, letting the
assistant gather a corpus in-session instead of you running `ietf-llm <name>` in a shell. The
instructions that the MCP server give to clients tell them how to use it.

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


## 4. Installing the IETF skills (optional)

You can install the bundled IETF skills into every supported agent harness:

```bash
ietf-llm --install-skills
```

Today that is `ietf-interpreting` and `ietf-contributing` (the read- and write-side norms the
server also serves via `read_ietf_interpretation_norms` / `read_ietf_participation_norms`),
`ietf-reviewing` (how to review an Internet-Draft), and `ietf-http` (BCP 56 / RFC 9205 guidance
for specs built on HTTP) — but the set is whatever [mnot/ietf-skill](https://github.com/mnot/ietf-skill)
publishes at the pinned tag, not a fixed list. This is a convenience; installing them from that
repo yourself is equivalent. Re-run after upgrading to pick up a newer pin.

**On WSL**, running this inside the distro also detects and installs into a
Windows-native Claude Code/Codex CLI/Gemini CLI/opencode, if present — WSL and
Windows don't share a home directory, so this is the only way a WSL-installed
`ietf-llm` can reach them. (This does not cover Claude Desktop, which has no
skills directory of its own — see the WSL note under Claude Desktop above.)


## Tuning the MCP server

Each tool call has a server-side deadline so a stuck call fails fast with a clear message rather
than hanging until the client's timeout. It defaults to 120 seconds; override (or disable, with
`0`) by setting `IETF_LLM_TOOL_TIMEOUT` in the server's environment — e.g. add `"env":
{"IETF_LLM_TOOL_TIMEOUT": "180"}` to a client's JSON config.

`start_gather` and `gather_status` block for up to `IETF_LLM_GATHER_MAX_WAIT` seconds
(default `10`) so a quick gather finishes in one call — but a tool call left outstanding
much beyond ~10s degrades some MCP clients. If your client turns choppy or stops
responding while a gather runs, set `IETF_LLM_GATHER_MAX_WAIT` lower, or to `0` to make
both tools return immediately (the model then polls `gather_status` without ever
blocking) — e.g. `"env": {"IETF_LLM_GATHER_MAX_WAIT": "0"}`.
