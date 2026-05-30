# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/)
Working Group's public record — charter, drafts, RFCs, meeting agendas,
minutes, slides, transcripts, mailing list archives, and GitHub issues —
for use with LLM-based tools.

> **Note:** This package was previously published as `ietf-notebook`.
> That distribution is deprecated. See
> [Migrating from `ietf-notebook`](#migrating-from-ietf-notebook).

## What it's for

A working group's history is spread across mailing list archives,
Datatracker, GitHub, and meeting materials — too much to hold in your
head, and too scattered to search well by hand. With the record
gathered into one queryable corpus, an LLM can help you:

- **Get up to date with the state of discussions** — what's open,
  what was recently decided, where a debate currently stands.
- **Summarise the arguments already made** about an issue — every
  distinct position on a topic, who holds it, and how the chairs
  ruled.
- **Formulate a new proposal** — surface the objections raised
  against similar ideas before, so you can anticipate them.
- **Fact-check assertions** about what's happened so far —
  grounded in the actual list traffic and chair statements, not
  someone's recollection.

Two supported workflows:

1. **[Use it as an MCP server](#1-use-as-an-mcp-server)** — register
   `ietf-llm-mcp` with Claude, Codex, Gemini, Cursor, Zed, etc. and
   ask questions across any WG you've gathered.
2. **[Use it with NotebookLM](#2-use-with-notebooklm)** — export the
   gathered corpus as a directory of clean text files (or push directly
   to NotebookLM Enterprise) and ingest it as a notebook source set.

> Also works with [IRTF](https://irtf.org/) Research Groups. Pass the
> RG's shortname (e.g. `cfrg`, `hrpc`, `pearg`) anywhere this README
> says `<wg>`.

## Table of contents

- [What it's for](#what-its-for)
- [Installation](#installation)
  - [Shell completion](#shell-completion)
- [1. Use as an MCP server](#1-use-as-an-mcp-server)
  - [Register the server](#register-the-server)
  - [Gather a corpus](#gather-a-corpus)
- [2. Use with NotebookLM](#2-use-with-notebooklm)
  - [Gather a corpus](#gather-a-corpus-1)
  - [Export to a local directory](#export-to-a-local-directory)
  - [Export to NotebookLM Enterprise](#export-to-notebooklm-enterprise)
- [Reference](#reference)
  - [Commands](#commands)
  - [Gather options](#gather-options)
  - [Semantic search from the CLI](#semantic-search-from-the-cli)
- [Migrating from `ietf-notebook`](#migrating-from-ietf-notebook)

## Installation

```bash
pipx install ietf-llm
```

Behind a corporate firewall with TLS interception? Install with the
`certs` extra:

```bash
pipx install ietf-llm[certs]
```

### Shell completion

Optional. Add the line for your shell to its rc file to tab-complete
commands, flags, and cached WG names:

```bash
# bash — in ~/.bashrc
eval "$(ietf-llm --completion bash)"
```

```bash
# zsh — in ~/.zshrc
eval "$(ietf-llm --completion zsh)"
```

```fish
# fish — in ~/.config/fish/config.fish
ietf-llm --completion fish | source
```

---

## 1. Use as an MCP server

`ietf-llm-mcp` is a stdio [Model Context Protocol](https://modelcontextprotocol.io/)
server that exposes the local corpus to any MCP-capable agent. Set up
once, gather each WG you care about once, then ask questions
indefinitely.

### Register the server

Pick your client. The snippets below are correct as of writing — if
your client has changed since, its own MCP docs are authoritative.

**Gotcha (all clients):** if `ietf-llm-mcp` was installed via `pipx`,
the binary is on your shell `PATH` but may not be on the `PATH`
inherited by a GUI app launched from Finder / Spotlight / Explorer.
Use the absolute path (`which ietf-llm-mcp`) if the client can't find
the command.

#### Claude Code

```bash
claude mcp add ietf-llm -- ietf-llm-mcp
```

Also install the bundled skill so Claude knows how to drive the tools
well (digests before raw reads, search before slurping mailing-list
files, etc.):

```bash
ietf-llm --install-claude-skill
```

Re-run after upgrading the package to pick up improvements.

#### Claude Desktop

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

#### Codex CLI (OpenAI)

`~/.codex/config.toml`:

```toml
[mcp_servers.ietf-llm]
command = "ietf-llm-mcp"
```

#### Gemini CLI

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

#### opencode

`~/.config/opencode/opencode.json` (or `opencode.json` in your project
root):

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

#### Cursor

In-app MCP settings panel, or `~/.cursor/mcp.json` (global) or
`.cursor/mcp.json` (per-project):

```json
{
  "mcpServers": {
    "ietf-llm": {
      "command": "ietf-llm-mcp"
    }
  }
}
```

#### Zed

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

### Gather a corpus

Gather from the CLI, once per corpus. Settings persist, so refreshing
is a bare re-run (`ietf-llm httpbis`), and the semantic index updates
incrementally each time.

```bash
ietf-llm httpbis --github httpwg/http-core --github httpwg/http-extensions
```

A corpus doesn't have to be a Working Group — the name is classified
automatically:

| Command | Corpus |
|---|---|
| `ietf-llm httpbis` | a WG / RG / editorial WG / BoF: charter, drafts, meetings, ballots, list |
| `ietf-llm last-call` | a standalone mailing list (any archived at mailarchive.ietf.org — IETF, IRTF, or RFC-Editor) |
| `ietf-llm rfced --mailing-list rswg@rfc-editor.org` | a named list corpus (the address domain is optional) |
| `ietf-llm new-ids --new-drafts --months 1` | new Internet-Drafts in a rolling window |
| `ietf-llm mnot --author mnot@mnot.net` | every draft a person has authored |

Everything lands in `~/.cache/ietf-llm/<name>/`, which the MCP server
reads. See [Gather options](#gather-options) for the full flag set.

---

## 2. Use with NotebookLM

NotebookLM ingests a corpus as a set of source files. `ietf-llm-export`
turns the gathered cache into an upload-ready directory, or pushes
straight to a NotebookLM Enterprise notebook.

> **Workflow note:** export always produces a complete fresh dump.
> Create a new notebook on each refresh rather than trying to merge
> updates into an existing one.

### Gather a corpus

Same as the MCP path; add `--no-embed` to skip the local index
(NotebookLM does its own):

```bash
ietf-llm httpbis --no-embed \
    --github httpwg/http-core --github httpwg/http-extensions
```

### Export to a local directory

```bash
ietf-llm-export httpbis --destination ~/notebooklm/httpbis
```

Drag the directory's contents into NotebookLM as sources. Per-thread
mailing list conversations and per-issue GitHub records are bundled
by year / repo to stay under NotebookLM's 50-source free / 300-source
Plus limit.

### Export to NotebookLM Enterprise

If you have Google Workspace Enterprise with NotebookLM enabled,
`ietf-llm-export` can create the notebook and upload sources directly:

```bash
ietf-llm-export httpbis --create my-gcp-project-id
```

One-time setup:

1. **Google Cloud Project** with the **Discovery Engine API** enabled.
2. **OAuth credentials**: create an "OAuth 2.0 Client ID" (Desktop
   App) in the [Cloud Console](https://console.cloud.google.com/apis/credentials).
3. **Save the JSON** as `client_secrets.json` in
   `~/.config/ietf-llm/` (or pass `--credentials-file PATH`).

First run opens a browser to authorise; the token is cached at
`~/.config/ietf-llm/token.json`.

Per-WG export settings are persisted at
`~/.config/ietf-llm/<wg>/export.json` — subsequent runs of the same
mode need only `ietf-llm-export <wg>`.

---

## Reference

### Commands

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | Gather / refresh a corpus | network | cache |
| `ietf-llm-export` | Mirror cache to dir, or push to NotebookLM Enterprise | cache | dir / NotebookLM |
| `ietf-llm-search` | Semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | Expose the cache to MCP clients | cache | stdio (MCP) |

All four are independent. The cache (`~/.cache/ietf-llm/<wg>/`) is
the single source of truth; everything else reads from it.

### Gather options

```bash
ietf-llm [OPTIONS] <name>
```

`<name>` is the corpus to gather, classified automatically:

- a **Working Group / Research Group / editorial WG / BoF** shortname
  (`httpbis`, `cfrg`, `rswg`) — gathered in full (charter, drafts,
  meetings, ballots, mailing list);
- a **mailing list** archived at mailarchive.ietf.org — IETF, IRTF,
  or RFC-Editor (`last-call`, `irtf-discuss`, `rfc-interest`) — that
  list on its own;
- any other **label** given explicit sources (`--draft` /
  `--mailing-list` / `--github` / `--new-drafts` / `--author`);
- prefix with `x-` to skip the Datatracker group lookup entirely (a
  fully manual corpus).

A name that is none of these and has no configured sources is rejected
as a likely typo.

**Sources** (what to gather; all repeatable / persisted):

- `--github OWNER/REPO` — a GitHub repo whose issues to include.
- `--draft DRAFT-NAME` — an extra Internet-Draft to track, beyond a
  WG's own documents. Version suffix stripped; every revision gathered.
- `--mailing-list LIST` — an extra list to sync (any archived at
  mailarchive.ietf.org). A bare name or a full address; the domain is
  optional and ignored (`rswg`, `rswg@rfc-editor.org`).
- `--new-drafts` — subscribe to *new* Internet-Drafts: every `-00`
  submitted within `--months` (rolling window; drafts age out).
- `--author PERSON` — every draft `PERSON` authored. `PERSON` is an
  email (`mnot@mnot.net`, recommended), a Datatracker person id, or an
  exact full name. Drafts only.
- `--add-mentioned-drafts` — also pull drafts the corpus's
  threads/issues mention but don't already include. Sticky.

**Scope & filtering:**

- `--months N` — months of mailing list / meeting / new-draft history
  (default 12).
- `--github-label LABEL` / `--exclude-github-label LABEL` — include /
  exclude issues by label.

**Digests & search index:**

- `--summarize` / `--summarize-model MODEL` — add LLM-generated
  one-liners to digests via the `llm` package.
- `--no-embed` — skip the semantic search index (it backs
  `ietf-llm-search` and the MCP `search_corpus` tool). On by default,
  incremental.
- `--embed-model MODEL` — embedding model id (default: a small local
  model).
- `--rebuild-embeddings` — drop and re-embed everything instead of the
  incremental update.

**Cache & config:**

- `--list` — list cached corpora (name, kind, status, last-gathered),
  then exit.
- `--clear-cache` — wipe this corpus's cache and re-download.
- `--clear-config` — clear this corpus's persisted config.
- `--quiet` / `--verbose`.

Per-corpus settings are persisted at
`~/.config/ietf-llm/<name>/gather.json`.

**GitHub auth.** Set `GITHUB_TOKEN` on the gather invocation (a fine-
scoped read-only token is plenty); without one you'll hit anonymous
API rate limits quickly on large WGs. Prefer inline-passing over
exporting in your shell rc so the token doesn't leak into every other
subprocess:

```bash
GITHUB_TOKEN=ghp_... ietf-llm httpbis
# or, from a secret manager:
GITHUB_TOKEN=$(security find-generic-password -s github-readonly -w) \
    ietf-llm httpbis
```

### Semantic search from the CLI

```bash
ietf-llm-search httpbis "skepticism about cookie partitioning" -k 8
```

Chunks are content-aware: one chunk per mailing list message, one per
issue comment, and a windowed slice of drafts/RFCs/transcripts. The
index lives at `~/.cache/ietf-llm/<wg>/embeddings.db` and updates
incrementally on each gather.

Default model: **`sentence-transformers/BAAI/bge-small-en-v1.5`** —
small (~33M params), MPS-accelerated, runs entirely on your machine.
Override with `--embed-model <id>` for any model the `llm` package
recognises.

---

## Migrating from `ietf-notebook`

If you previously used the `ietf-notebook` distribution:

```bash
pipx uninstall ietf-notebook
pipx install ietf-llm
```

Cache and config directories changed names. To preserve a gathered
cache, move it by hand:

```bash
mv ~/.cache/ietf-notebook  ~/.cache/ietf-llm
mv ~/.config/ietf-notebook ~/.config/ietf-llm
```

Otherwise the old directories are simply ignored.

### Command renames

| Before | After |
|---|---|
| `ietf-notebook <wg>` | `ietf-llm <wg>` |
| (no equivalent) | `ietf-llm-export <wg>` (split out) |
| (no equivalent) | `ietf-llm-search <wg> <query>` (new) |
| (no equivalent) | `ietf-llm-mcp` (new) |

### Flags moved off the gather CLI

These now live on `ietf-llm-export`:

| Old: `ietf-notebook <wg> ...` | New |
|---|---|
| `--destination DIR` | `ietf-llm-export <wg> --destination DIR` |
| `--create GCP_PROJECT` | `ietf-llm-export <wg> --create GCP_PROJECT` |
| `--credentials-file PATH` | `ietf-llm-export <wg> --credentials-file PATH` |
| `--token-file PATH` | `ietf-llm-export <wg> --token-file PATH` |

If you pass any of these to `ietf-llm`, you'll get a redirect error.

### `--update` is gone

The gather CLI is now idempotent — re-run it whenever you want fresh
data. The export CLI always produces a complete fresh dump; for
NotebookLM, create a new notebook each refresh rather than trying to
merge updates.
