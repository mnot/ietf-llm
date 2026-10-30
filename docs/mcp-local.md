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

[pipx](https://pipx.pypa.io/stable/) is recommended:

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The `[local-embeddings]` extra installs the on-device embedding backend that search needs. An Intel
Mac needs Python 3.10–3.12; see [Model backends](models.md).

Behind a TLS-intercepting firewall, outbound connections verify against your OS trust store, so a
root your machine already trusts works. If the proxy's root is not installed there, point
`SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at its PEM.

## 2. Register the server with your client

Register `ietf-llm-mcp` in your client's config as below (clients alphabetical). The snippets are
correct as of writing; your client's own MCP docs are authoritative if it has changed.

The binary may be on your shell `PATH` but not on the `PATH` your client inherits. If the client
cannot find the command, use the absolute path from `which ietf-llm-mcp`.

The server sends its routing and norms guidance to every client in the MCP `instructions` field, so
no skill is needed for routing.

### Claude Code

```bash
claude mcp add ietf-llm -- ietf-llm-mcp
```

Native Windows Claude Code with `ietf-llm` installed in WSL needs a wrapper; see
[Under WSL](#under-wsl).

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

Quit and relaunch Claude Desktop; it reads the config only at startup. Its 'Chat' tab seems to have
an MCP performance issue that `IETF_LLM_GATHER_MAX_WAIT=0` may help with; see
[Tuning](#tuning-the-mcp-server). Under WSL, see [Under WSL](#under-wsl).

### Codex CLI (OpenAI)

`~/.codex/config.toml`:

```toml
[mcp_servers.ietf-llm]
command = "ietf-llm-mcp"
```

### Cursor

The in-app MCP settings panel, or `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project),
using the same `mcpServers` JSON as Claude Desktop.

### Gemini CLI

`~/.gemini/settings.json`, using the same `mcpServers` JSON as Claude Desktop.

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

### Under WSL

This applies when `ietf-llm` is installed inside a WSL distro but the client is a native Windows
process: Claude Desktop always, and Claude Code, Codex CLI, Gemini CLI and opencode when launched
from a Windows terminal. A Windows process cannot exec a Linux binary, so route the command through
`wsl.exe` with the absolute Linux path (`which ietf-llm-mcp` inside the distro). Each client's
config file stays on the Windows side.

```json
// Claude Desktop — %APPDATA%\Claude\claude_desktop_config.json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "wsl.exe",
      "args": ["-d", "<YourDistroName>", "--cd", "~", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"]
    }
  }
}
```

```powershell
# Claude Code — run from a Windows shell
claude mcp add ietf-llm -- wsl.exe -d <YourDistroName> --cd "~" -e /home/<you>/.local/bin/ietf-llm-mcp
```

```toml
# Codex CLI — %USERPROFILE%\.codex\config.toml
[mcp_servers.ietf-llm]
command = "wsl.exe"
args = ["-d", "<YourDistroName>", "--cd", "~", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"]
```

Gemini CLI (`%USERPROFILE%\.gemini\settings.json`) takes the Claude Desktop JSON above. opencode
(`%USERPROFILE%\.config\opencode\opencode.json`) takes the same command and arguments as one
`"command"` array:

```json
"command": ["wsl.exe", "-d", "<YourDistroName>", "--cd", "~", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"]
```

`-d <YourDistroName>` is optional with one distro (`wsl -l -v` lists them). Keep `--cd ~`: otherwise
`wsl.exe` translates the Windows working directory into the distro, which fails if it cannot be
translated.

**Environment variables need `WSLENV`.** A client `"env"` block sets variables on the Windows
`wsl.exe` process, and they do not cross into the distro. Nor does `~/.bashrc` help, since `wsl.exe
-e` runs the binary with no shell. Name each variable in
[`WSLENV`](https://learn.microsoft.com/en-us/windows/wsl/filesystems#share-environment-variables-between-windows-and-wsl)
(or hardcode it in a wrapper script that `-e` points at):

```json
      "env": {
        "IETF_LLM_GATHER_MAX_WAIT": "0",
        "WSLENV": "IETF_LLM_GATHER_MAX_WAIT"
      }
```

**Skills are separate.** `ietf-llm --install-skills` inside the distro also installs into these
Windows-side harnesses, but it does not register the MCP server with them; that is the per-client
config above.

A client that itself runs in WSL (for example VS Code with Remote-WSL, or any tool launched from a
WSL terminal) sees `ietf-llm-mcp` on its own `PATH`, so the plain configuration applies with no
`wsl.exe` wrapper.

## 3. Gather

The tools read corpora gathered from IETF servers. Gather on the command line with `ietf-llm <name>`
(see [Gathering a corpus](gathering.md)), or ask the assistant: a local stdio server registers
`start_gather` and `gather_status` by default, and the instructions it sends tell the assistant how
to use them.

The RFC tools need data that no gather brings; run `ietf-llm --init` before using them on a machine
that has not gathered yet (see [Gathering](gathering.md)).

To turn in-session gather off, for example when the cache is on a read-only mount, set
`IETF_LLM_ENABLE_GATHER=0` in the server's environment:

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

```bash
ietf-llm --install-skills
```

This installs the bundled IETF skills into every supported agent harness it finds (Claude Code,
Codex CLI, Gemini CLI, opencode). The set is whatever [mnot/ietf-skill](https://github.com/mnot/ietf-skill)
publishes at the pinned tag; installing from that repo yourself is equivalent. Re-run after
upgrading to pick up a newer pin. Inside WSL it also installs into Windows-native harnesses, since
WSL and Windows do not share a home directory (see [Under WSL](#under-wsl)).

## Tuning the MCP server

Set these in the server's environment, for example `"env": {"IETF_LLM_TOOL_TIMEOUT": "180"}` in a
client's JSON config.

- `IETF_LLM_TOOL_TIMEOUT` — the per-call deadline in seconds (default `120`; `0` disables), so a
  stuck call fails with a clear message before the client's own timeout.
- `IETF_LLM_GATHER_MAX_WAIT` — how long `start_gather` and `gather_status` block, in seconds
  (default `10`). A call outstanding much longer degrades some clients. If the client turns choppy or stops responding during a gather, lower this, or set
  `0` so both tools return immediately and the model polls `gather_status`.

The full list of server variables is in [Running the MCP server over HTTP](mcp-server.md#configuration-reference).
