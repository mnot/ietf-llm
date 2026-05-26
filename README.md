# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/)
Working Group's public record — charter, drafts, RFCs, meeting minutes,
slides, transcripts, mailing list archives, and GitHub issues — for use
with LLM-based tools.

The cache can be queried directly via [Model Context Protocol](https://modelcontextprotocol.io/)
(e.g. from Claude Desktop or Claude Code), searched semantically from the
command line, or exported as a directory of clean text files for
[NotebookLM](https://notebooklm.google.com/).

> **Note:** This package was previously published as `ietf-notebook`.
> That distribution is deprecated and no longer maintained. The rename
> reflects a broader purpose (the tool now feeds LLM consumers
> generally, not just NotebookLM) and a restructured CLI surface — see
> "Migrating from ietf-notebook" below.

## Installation

```bash
pipx install ietf-llm
```

### Certificate Errors

If you encounter SSL or certificate errors (common behind corporate firewalls), install with the `certs` option:

```bash
pipx install ietf-llm[certs]
```

## Quick Start: Using with Claude via MCP

The fastest way to use `ietf-llm` is to register its MCP server with
Claude (Code or Desktop). Once set up, Claude can read digests, search,
and inspect any Working Group you've gathered — no per-WG configuration
on the Claude side.

### a) One-time MCP setup

1. **Install with the search + MCP extras** so embeddings and the MCP
   server are both available:

   ```bash
   pipx install 'ietf-llm[search,mcp]'
   ```

   (If you already have it installed: `pipx install --force 'ietf-llm[search,mcp]'`.)

2. **Register the MCP server** with your Claude client. For Claude Code:

   ```bash
   claude mcp add ietf-llm -- ietf-llm-mcp
   ```

   For Claude Desktop, edit `claude_desktop_config.json` (create it if it
   doesn't exist):

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
   If `ietf-llm-mcp` isn't on the PATH that Claude Desktop sees
   (common with `pipx`), use the absolute path instead, e.g.
   `"command": "/Users/you/.local/bin/ietf-llm-mcp"` (find it with
   `which ietf-llm-mcp`).

3. **(Recommended)** Install the bundled skill so Claude knows the
   right way to use the corpus (digests first, search before reading,
   no slurping raw mbox files):

   ```bash
   git clone https://github.com/mnot/ietf-llm /tmp/ietf-llm
   cp -r /tmp/ietf-llm/skill/ietf-llm ~/.claude/skills/
   ```

4. **(Optional)** Set `GITHUB_TOKEN` in your shell environment to avoid
   GitHub API rate limits during gather:

   ```bash
   export GITHUB_TOKEN=ghp_...
   ```

### b) Gathering a Working Group

Gathering happens via the CLI (it's a slow, network-heavy job that's not
appropriate to run silently from a chat tool). Do it once per WG you
want Claude to be able to query:

```bash
ietf-llm httpbis \
    --github httpwg/http-core \
    --github httpwg/http-extensions \
    --embed
```

What each flag does:

- `--github org/repo` — GitHub repos whose issues should be gathered.
  Repeat for each repo. Persisted after first run, so future updates
  don't need it.
- `--embed` — builds the local semantic search index that backs
  `search_corpus` in MCP. **Required if you want Claude to search the
  corpus.** Uses a small local model (no API key) on first run; this
  downloads ~130 MB of model weights once and reuses them after.

Everything is written to `~/.cache/ietf-llm/<wg>/` — the cache, the
digests, the embedding DB. The MCP server, `ietf-llm-search`, and
`ietf-llm-export` all read from there. **For the Claude / MCP workflow,
that's all you need to do** — there is no separate "destination"
directory to manage.

The first gather takes a few minutes (mailing list IMAP fetch
dominates, then embedding).

Now in Claude:

> "What's open in httpbis right now?"
> "Anyone on the list raised concerns about cookie partitioning?"
> "Summarize what draft-ietf-httpbis-rfc6265bis says about §5.4."

Claude will use `list_working_groups`, `read_digest`, and `search_corpus`
to answer without you having to point at any files.

### c) Updating a Working Group

Just re-run the gather. All WG-specific config (GitHub repos, embed
choice, etc.) is remembered from the first run:

```bash
ietf-llm httpbis
```

- Embedding is incremental — only changed files are re-embedded, so
  update runs are fast even on large WGs.
- Add `--summarize` if you want LLM-generated one-line summaries
  refreshed in the digest files (requires an `llm`-configured model).

Run this on a cron or whenever you want fresh data. Claude picks up the
new state automatically on its next MCP call — nothing to restart.

If you're also using NotebookLM (the section below) and want it to see
the update, the recommended workflow is to **create a fresh notebook
each time** rather than try to diff into an existing one. Re-run
`ietf-llm-export <wg> --destination <dir>` (or `--create <gcp_project>`)
to get a clean export for the new notebook.

---

## The Tools

`ietf-llm` ships four console scripts, each with a single job:

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | Gather / refresh a WG | network | cache |
| `ietf-llm-export` | Mirror cache to dir, or push to NotebookLM Enterprise | cache | dir / NotebookLM |
| `ietf-llm-search` | Semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | Expose the cache to MCP clients | cache | stdio (MCP) |

All four are independent. The cache (`~/.cache/ietf-llm/<wg>/`) is the
single source of truth; everything else reads from it.

## Usage: `ietf-llm` (gather)

```bash
ietf-llm [OPTIONS] _wg_shortname_
```

Populates `~/.cache/ietf-llm/<wg>/` with the WG charter, drafts, RFCs,
meeting materials, transcripts, mailing list archives, and GitHub
issues. Generates the three digest files (`_index.md`, `_issues.md`,
`_threads.md`). Optionally builds the embedding index.

Per-WG options are persisted at
`~/.config/ietf-llm/<wg>/gather.json`, so subsequent runs need only
`ietf-llm <wg>`. Use `--clear-config` to reset.

Options:
- `wg_shortname` (positional) — e.g. `httpbis`, `quic`, `tls`.
- `--github OWNER/REPO` — repeat for each repo whose issues to gather.
- `--github-label LABEL` / `--exclude-github-label LABEL` — repeat for
  multiple labels.
- `--months N` — months of mailing list / meeting history (default 12).
- `--summarize` / `--summarize-model MODEL` — add LLM-generated
  one-liners to digests via the `llm` package.
- `--embed` / `--embed-model MODEL` — build / refresh the semantic
  search index (required for `ietf-llm-search` and the MCP
  `search_corpus` tool).
- `--rebuild-embeddings` — with `--embed`, drop and re-embed instead of
  incremental update.
- `--clear-cache` — wipe the local cache for this WG and re-download.
- `--clear-config` — clear persisted config for this WG
  (both gather and export scopes).
- `--quiet` / `--verbose`.

### Default behaviour

- **File caching**: documents are collected under
  `~/.cache/ietf-llm/<wg>/files/`. Existing cached files are skipped
  unless `--clear-cache` is used.
- **Mailing list discovery**: the list address is looked up
  automatically from the Datatracker.
- **IMAP retrieval**: mailing list archives are fetched via IMAP from
  `imap.ietf.org` and cached at `~/.cache/ietf-llm/<wg>/imap-cache/`.
- **GitHub strategy**: the tool first checks for `archive.json` on the
  `gh-pages` branch of each repo, then falls back to the API.
- **GitHub auth**: set `GITHUB_TOKEN` to avoid API rate limits.
- **Transcripts**: fetched from the `ietf-minutes-data` repo and cached
  at `~/.cache/ietf-llm/<wg>/transcript-cache/`.

## Usage: `ietf-llm-export`

```bash
ietf-llm-export <wg> --destination <dir>          # local mirror
ietf-llm-export <wg> --create <GCP_PROJECT_ID>    # NotebookLM Enterprise
```

Both modes always produce a complete, fresh export — there is no
incremental / delta mode. The recommended workflow is to **create a
new NotebookLM notebook on each update** rather than try to merge
changes into an existing one.

Per-WG options are persisted at
`~/.config/ietf-llm/<wg>/export.json`, so subsequent runs of the same
mode need only `ietf-llm-export <wg>`. The two modes are mutually
exclusive — switch by passing the new flag explicitly, or
`ietf-llm <wg> --clear-config` to reset everything.

### NotebookLM Enterprise mode (`--create`)

If you have a Google Workspace Enterprise account with NotebookLM
enabled, `ietf-llm-export <wg> --create <GCP_PROJECT_ID>` will create
a notebook and upload every cached file as a source.

Requirements:

1. **Google Cloud Project** with the **Discovery Engine API** enabled.
2. **OAuth Credentials**: an "OAuth 2.0 Client ID" (Type: Desktop App)
   from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
3. **Client Secrets**: save the JSON as `client_secrets.json` in
   `~/.config/ietf-llm/` (or pass `--credentials-file PATH`).

On first run, a browser window opens to authorise the app. The token
is cached at `~/.config/ietf-llm/token.json` (or `--token-file PATH`).

## Digest Files

Every gather produces three digest files in the cache, designed to be the
entry points for navigating the corpus (especially when using an LLM
assistant):

- `<wg>-_index.md` — landing page with a categorized inventory of all files.
- `<wg>-_issues.md` — one row per GitHub issue (state, title, labels,
  comments, last updated), sorted open-first.
- `<wg>-_threads.md` — one row per mailing list thread (normalized subject,
  message count, participants, date range).

These are generated deterministically from the cache and add no API cost.
Pass `--summarize` to also include one-line LLM summaries per issue and
thread; this requires the [`llm`](https://llm.datasette.io/) package
(install with `pipx inject ietf-llm llm`, or use the `summarize`
extra) and a configured model. Override the model with
`--summarize-model claude-haiku-4-5` (or any other model id known to `llm`).

## Semantic Search

Pass `--embed` at gather time to build a local embedding index alongside the
text files:

```bash
ietf-llm httpbis --embed
```

Then search from the command line:

```bash
ietf-llm-search httpbis "skepticism about cookie partitioning" -k 8
```

Chunks are content-aware: one chunk per mailing list message, one per
GitHub issue, and a windowed slice of drafts/RFCs/transcripts. The index
lives at `~/.cache/ietf-llm/<wg>/embeddings.db` and updates
incrementally on the next `--embed` run. Requires the `search` extra:

```bash
pipx inject ietf-llm 'ietf-llm[search]'
```

The default model is **`sentence-transformers/BAAI/bge-small-en-v1.5`** —
a small (~33M params), MPS-accelerated local model that runs entirely on
your machine with no API calls. It's downloaded automatically on first use.
Override with `--embed-model <id>` to use any model `llm` knows about
(e.g. `3-small` for OpenAI, `sentence-transformers/nomic-ai/nomic-embed-text-v1.5`
for a higher-quality local model with 8k context).

## MCP Server

`ietf-llm-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io/)
server that exposes the gathered corpus to MCP clients (Claude Desktop,
Claude Code, etc.). Tools:

- `list_working_groups()` / `list_files(wg)`
- `read_digest(wg, kind)` — `index`, `issues`, or `threads`
- `search_corpus(wg, query, k)` — semantic search
- `get_chunk_text(wg, file, chunk_idx)` — full text of a chunk returned by search
- `read_file_section(wg, file, start_line, max_lines)` — bounded raw read,
  capped at 2000 lines per call

Install the extra and register the server with your client:

```bash
pipx inject ietf-llm mcp
# Claude Code:
claude mcp add ietf-llm -- ietf-llm-mcp
```

## Using with Claude

A Claude Code skill is included at `skill/ietf-llm/SKILL.md`. Copy it
into your skills directory to let Claude gather and query WG corpora on your
behalf:

```bash
cp -r skill/ietf-llm ~/.claude/skills/
```

The skill teaches Claude to drive the CLI, read the digest files first,
prefer `ietf-llm-search` / the MCP tools over raw reads, and avoid
pulling multi-MB mailing-list or issue dumps into context. For broad
exploratory Q&A across the full corpus, NotebookLM remains a good fit
(the destination directory is designed to be uploaded as-is).

## Migrating from `ietf-notebook`

If you previously used the `ietf-notebook` distribution, here's what
changed:

### Uninstall the old, install the new

```bash
pipx uninstall ietf-notebook
pipx install 'ietf-llm[search,mcp]'
```

### Move the cache and config (optional)

The cache and config directories changed names. There is no automatic
migration; if you want to preserve a gathered cache, move it by hand:

```bash
mv ~/.cache/ietf-notebook  ~/.cache/ietf-llm
mv ~/.config/ietf-notebook ~/.config/ietf-llm
```

If you don't, the old directories are simply ignored and the new tool
starts with an empty cache.

### Command renames

| Before | After |
|---|---|
| `ietf-notebook <wg>` | `ietf-llm <wg>` |
| (no equivalent) | `ietf-llm-export <wg>` (split out — see below) |
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

If you pass any of these to `ietf-llm`, you'll get a redirect error
explaining where they went.

### `--update` is gone

The previous "only mirror files that changed in this run" behaviour has
been removed. The gather CLI is now idempotent — re-run it whenever you
want fresh data, no special flag needed:

```bash
ietf-llm httpbis              # gather or refresh
```

The export CLI always produces a complete fresh dump. For NotebookLM,
the recommended workflow is to **create a new notebook on each update**
rather than try to merge changes into an existing one.

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
