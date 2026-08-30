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

Running inside a WSL distro, this just works. Running Claude Code as a
*native Windows* process against an `ietf-llm` installed in WSL needs a
wrapper — see [Under WSL](#under-wsl).

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

**On WSL:** Claude Desktop is a native Windows process and cannot exec a
Linux binary, so a `pipx install` inside a WSL distro is not enough on its
own — see [Under WSL](#under-wsl) below.


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

### Under WSL

Only relevant if `ietf-llm` is installed **inside a WSL distro** but the client
is a **native Windows** process (Claude Desktop always is; Claude Code, Codex
CLI, Gemini CLI and opencode are when launched from a Windows terminal rather
than a WSL one). A Windows process cannot exec a Linux binary, so `"command":
"ietf-llm-mcp"` will not resolve — route it through `wsl.exe` instead, giving
the absolute Linux path (find it with `which ietf-llm-mcp` *inside* the
distro). Each client's config file still lives on the Windows side.

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

```json
// Gemini CLI — %USERPROFILE%\.gemini\settings.json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "wsl.exe",
      "args": ["-d", "<YourDistroName>", "--cd", "~", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"]
    }
  }
}
```

```json
// opencode — %USERPROFILE%\.config\opencode\opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ietf-llm": {
      "type": "local",
      "command": ["wsl.exe", "-d", "<YourDistroName>", "--cd", "~", "-e", "/home/<you>/.local/bin/ietf-llm-mcp"],
      "enabled": true
    }
  }
}
```

`-d <YourDistroName>` is optional if you only have one distro (`wsl -l -v` from
PowerShell lists them). `--cd ~` is worth keeping: `wsl.exe` otherwise
translates the launching process's Windows working directory into the distro,
which fails if that directory isn't translatable.

**Environment variables need `WSLENV`.** A `"env"` block alone is not enough —
those variables are set on the Windows `wsl.exe` process and are not forwarded
across the boundary. Nor does setting them inside the distro help: `wsl.exe -e`
execs the binary directly with no shell, so `~/.bashrc` is never sourced.
Name each variable in
[`WSLENV`](https://learn.microsoft.com/en-us/windows/wsl/filesystems#share-environment-variables-between-windows-and-wsl)
to carry it across (or hardcode it into a wrapper script you point `-e` at).
This matters for the `IETF_LLM_GATHER_MAX_WAIT` workaround above, which would
otherwise silently do nothing:

```json
      "env": {
        "IETF_LLM_GATHER_MAX_WAIT": "0",
        "WSLENV": "IETF_LLM_GATHER_MAX_WAIT"
      }
```

**Skills are separate.** Running `ietf-llm --install-skills` inside the distro
also installs into these Windows-side harnesses (see
[step 4](#4-installing-the-ietf-skills-optional)), but it cannot register the
MCP server for you — that is the per-client config above. Skills installed
without it leave the harness with no route to `ietf-llm-mcp`.

**No split to bridge if the client itself runs in WSL.** The common VS Code +
Remote-WSL setup runs the editor and all its subprocesses inside the distro, so
its MCP server process is a WSL-side Linux process that sees `ietf-llm-mcp` on
its own `PATH` — the plain `claude mcp add ietf-llm -- ietf-llm-mcp` applies,
with no `wsl.exe` wrapper. Same for any of these tools run from a WSL terminal.


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
`ietf-llm` can reach them. Skills are all it installs: registering the MCP
server with those Windows-side harnesses is a separate step, and without it
they have no route to `ietf-llm-mcp` — see [Under WSL](#under-wsl). (Claude
Desktop has no skills directory of its own, so it is unaffected either way.)


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
