# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/)
Working Group's public record — charter, drafts, RFCs, meeting minutes,
slides, transcripts, mailing list archives, and GitHub issues — for use
with LLM-based tools.

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

> **Note:** This package was previously published as `ietf-notebook`.
> That distribution is deprecated. See
> [Migrating from `ietf-notebook`](#migrating-from-ietf-notebook).

## Table of contents

- [Installation](#installation)
- [1. Use as an MCP server](#1-use-as-an-mcp-server)
  - [Register the server](#register-the-server)
  - [Gather a Working Group](#gather-a-working-group)
  - [Ask your agent](#ask-your-agent)
  - [Updating](#updating)
- [2. Use with NotebookLM](#2-use-with-notebooklm)
  - [Gather a Working Group](#gather-a-working-group-1)
  - [Export to a local directory](#export-to-a-local-directory)
  - [Export to NotebookLM Enterprise](#export-to-notebooklm-enterprise)
- [Reference](#reference)
  - [Commands](#commands)
  - [Gather options](#gather-options)
  - [Semantic search from the CLI](#semantic-search-from-the-cli)
  - [Digest files](#digest-files)
  - [MCP tools](#mcp-tools)
- [Migrating from `ietf-notebook`](#migrating-from-ietf-notebook)
- [Contributing](#contributing)

## Installation

```bash
pipx install ietf-llm
```

Behind a corporate firewall with TLS interception? Install with the
`certs` extra:

```bash
pipx install ietf-llm[certs]
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

### Gather a Working Group

Gathering is a slow, network-heavy job, so it runs from the CLI —
not silently from the agent. Do it once per WG you want to query:

```bash
ietf-llm httpbis \
    --github httpwg/http-core \
    --github httpwg/http-extensions \
    --embed
```

- `--github org/repo` — GitHub repos whose issues to include. Repeat
  per repo. Persisted, so future updates omit it.
- `--embed` — build the local semantic search index that backs the
  `search_corpus` MCP tool. **Required if you want the agent to
  search.** Downloads ~130 MB of model weights once on first run.

Everything goes to `~/.cache/ietf-llm/<wg>/`. The MCP server reads
from there — no separate destination to manage.

### Ask your agent

```text
"What's open in httpbis right now?"
"Anyone on the list raised concerns about cookie partitioning?"
"How did the debate on MLKEM evolve in TLS?"
```

The agent uses `list_working_groups`, `overview`, `read_digest`,
`search_corpus`, and `read_topic` to answer — no need to point at
files. See [MCP tools](#mcp-tools) for the full surface.

### Updating

Just re-run the gather. All per-WG settings (GitHub repos, embedding
choice) are remembered:

```bash
ietf-llm httpbis
```

Embedding is incremental — only changed files are re-embedded. Run on
a cron or whenever you want fresh data; the agent picks up the new
state on its next tool call.

---

## 2. Use with NotebookLM

NotebookLM ingests a corpus as a set of source files. `ietf-llm-export`
turns the gathered cache into an upload-ready directory, or pushes
straight to a NotebookLM Enterprise notebook.

> **Workflow note:** export always produces a complete fresh dump.
> Create a new notebook on each refresh rather than trying to merge
> updates into an existing one.

### Gather a Working Group

Same as the MCP path, but `--embed` is optional (NotebookLM does its
own indexing):

```bash
ietf-llm httpbis \
    --github httpwg/http-core \
    --github httpwg/http-extensions
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
| `ietf-llm` | Gather / refresh a WG | network | cache |
| `ietf-llm-export` | Mirror cache to dir, or push to NotebookLM Enterprise | cache | dir / NotebookLM |
| `ietf-llm-search` | Semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | Expose the cache to MCP clients | cache | stdio (MCP) |

All four are independent. The cache (`~/.cache/ietf-llm/<wg>/`) is
the single source of truth; everything else reads from it.

### Gather options

```bash
ietf-llm [OPTIONS] <wg_shortname>
```

- `--github OWNER/REPO` — repeat per GitHub repo whose issues to gather.
- `--draft DRAFT-NAME` — extra Internet-Draft to track, beyond the
  WG's auto-discovered documents (repeatable, persisted). Version
  suffix is stripped; every revision is gathered.
- `--mailing-list LIST` — extra IETF-hosted mailing list to sync,
  beyond the WG's auto-discovered one (repeatable, persisted).
  Accepts `foo` or `foo@ietf.org`.
- `--github-label LABEL` / `--exclude-github-label LABEL` — filter
  issues by label; repeatable.
- `--months N` — months of mailing list / meeting history (default 12).
- `--summarize` / `--summarize-model MODEL` — add LLM-generated
  one-liners to digests via the `llm` package.
- `--embed` / `--embed-model MODEL` — build / refresh the semantic
  search index (required for `ietf-llm-search` and the MCP
  `search_corpus` tool).
- `--rebuild-embeddings` — with `--embed`, drop and re-embed instead
  of incremental update.
- `--clear-cache` — wipe the cache for this WG and re-download.
- `--clear-config` — clear persisted config for this WG.
- `--quiet` / `--verbose`.

Per-WG settings are persisted at `~/.config/ietf-llm/<wg>/gather.json`.

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
incrementally on each `--embed` run.

Default model: **`sentence-transformers/BAAI/bge-small-en-v1.5`** —
small (~33M params), MPS-accelerated, runs entirely on your machine.
Override with `--embed-model <id>` for any model the `llm` package
recognises.

### Digest files

Every gather produces small markdown digests under
`~/.cache/ietf-llm/<wg>/files/digests/`:

- `index.md` — categorised inventory of all cached files.
- `issues.md` — one row per GitHub issue (state, title, labels,
  comments, last updated), sorted open-first.
- `threads.md` — one row per mailing list thread (subject, message
  count, participants, date range).
- `people.md` — participants with roles + message counts.
- `timeline.md` — chronological events (draft publications, issue
  open/close, meetings, polls, WGLC, …).

Generated deterministically from the cache. Pass `--summarize` to
also include LLM-generated one-liners per row.

### MCP tools

`ietf-llm-mcp` exposes:

- `list_working_groups()` — WGs gathered locally.
- `overview(wg)` — chairs, active drafts, top open issues, recent
  threads, latest meeting. First call for "tell me about X."
- `list_labels(wg)` — GitHub issue labels with frequencies.
- `list_files(wg, pattern?)` — file inventory with chunk counts.
- `read_digest(wg, kind, ...filters)` — `index` / `issues` /
  `threads` / `people` / `timeline`. Filters compose (state, label,
  author, role, since/until, event_kind, …).
- `search_corpus(wg, query, ...)` — semantic search with optional
  `state`, `label`, `file_pattern`, `since`/`until`, `sort="date"`,
  `group_by="file"`.
- `read_topic(wg, query, include_replies=False)` — chronological
  narrative view: full message bodies across threads and issues in
  date order, optionally walking reply descendants.
- `get_chunk_text(wg, file, chunk_idx, end_chunk_idx?)` — full text
  of one chunk (or a range).
- `get_chunks_batch(wg, [{file, chunk_idx, end_chunk_idx?}, …])` —
  multi-file batch fetch.
- `fetch_by_url(wg, url)` — resolve a GitHub or mail-archive URL to
  its cached content.
- `read_file_section(wg, file, start_line, max_lines)` — bounded raw
  read (default 400 lines, hard cap 5000).

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

## Contributing

Pull requests welcome. For major changes, please open an issue first.

[ARCHITECTURE.md](ARCHITECTURE.md) is the read-this-first for anyone
poking at the code: package layout, cache and config conventions,
data flow, and the key design decisions worth knowing before you
change anything.
