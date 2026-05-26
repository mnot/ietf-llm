# ietf-llm

Automate gathering of [NotebookLM](https://notebooklm.google.com/)-ready documents for an [IETF](https://www.ietf.org/) Working Group.

This tool gathers Working Group charters, drafts, meeting minutes, PDF slides, meeting transcripts, mailing list archives, and GitHub issues into a set of clean text files and PDFs suitable for ingestion into NotebookLM.

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

Gathering still happens via the CLI (it's a slow, network-heavy job
that's not appropriate to run silently from a chat tool). Do it once
per WG you want Claude to be able to query:

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

Everything is written to `~/.cache/ietf-llm/<wg>/` — the cache,
the digests, the embedding DB. The MCP server, `ietf-llm-search`,
and the NotebookLM uploader all read from there. **If you also want
a plain folder of text/md files to upload to NotebookLM by hand, add
`--destination ~/ietf/<wg>`** — that creates a mirror of the
human-readable files. It's optional and not needed for the Claude /
MCP workflow.

The first gather takes a few minutes (mailing list IMAP fetch
dominates, then embedding).

Now in Claude:

> "What's open in httpbis right now?"
> "Anyone on the list raised concerns about cookie partitioning?"
> "Summarize what draft-ietf-httpbis-rfc6265bis says about §5.4."

Claude will use `list_working_groups`, `read_digest`, and `search_corpus`
to answer without you having to point at any files.

### c) Updating a Working Group

Re-run with `--update` to pull new material since the last gather. All
the WG-specific config (destination, GitHub repos, etc.) is remembered:

```bash
ietf-llm --update httpbis --embed
```

- Only files that actually changed are mirrored to the destination.
- `--embed` is incremental — only changed files are re-embedded, so
  update runs are fast even on large WGs.
- Add `--summarize` if you want LLM-generated one-line summaries
  refreshed in the digest files (requires an `llm`-configured model).

Run this on a cron or whenever you want fresh data. Claude picks up the
new state automatically on its next MCP call — nothing to restart.

---

## Usage

### First Run

To start collecting documents for a Working Group:

```bash
ietf-llm [OPTIONS] _wg_shortname_
```

This populates the local cache at `~/.cache/ietf-llm/<wg>/` with the
WG charter, meeting minutes, slides, transcripts, mailing list archives,
and GitHub issues. The MCP server, `ietf-llm-search`, and `--create`
(NotebookLM Enterprise upload) all read from the cache.

If you want a separate clean folder of text/md files to upload to
NotebookLM by hand, add `--destination`:

```bash
ietf-llm --destination _destination_ _wg_shortname_
```

Then upload the files in _destination_ to NotebookLM.

Because `ietf-llm` persists Working Group configuration options, you don't need to specify them again for that Working Group. Use `--clear-config` to reset a group's configuration.

### Subsequent Updates

To update the documents, run with the --update flag.

```bash
ietf-llm --update _wg_shortname_
```

 _destination_ will only contain files that have changed since the last run. Upload the new and updated files to NotebookLM.


### Options

Working Group configuration:
- `wg_shortname`: IETF Working Group short name (e.g., `httpbis`).
- `--github`: GitHub org/repo for issues (can be specified multiple times).
- `--github-label`: Include only GitHub issues with this label (can be specified multiple times).
- `--exclude-github-label`: Exclude GitHub issues with this label (can be specified multiple times).
- `--months`: Number of months of mailing list history to fetch (default: 12).
- `--clear-config`: Clear and reset the persisted configuration for this Working Group.

Output control:
- `--destination`: Folder to populate with group records.
- `--create`: See "NotebookLM Export" below.
- `--clear-cache`: Clear the local file cache and re-download everything from scratch.
- `--update`: Only write updated files to destination. NOTE: the destination folder is emptied when using this flag.

General options:
- `--quiet`: No messages except for errors and the final resource summary.
- `--verbose`: Detailed progress reporting.


### Default Behavior

- **Mirroring Strategy**: By default, the `--destination` folder is updated with the latest versions of all files from the local cache. If `--update` is used, only files that have changed during the current run will be written there.
- **File Caching**: All documents are collected in `~/.cache/ietf-llm/[wg]/files/` to avoid redundant downloads.
- **Charters, Meetings, and Documents**: Existing files in the cache are skipped unless `--clear-cache` is used.
- **Mailing List Discovery**: The tool automatically finds the mailing list for the WG from the Datatracker.
- **IMAP Retrieval**: Mailing list archives are fetched via IMAP from `imap.ietf.org` and cached locally in `~/.cache/ietf-llm/{wg_name}/imap-cache/`.
- **GitHub Strategy**: The tool first checks for `archive.json` on the `gh-pages` branch.
- **Transcripts**: Meeting transcripts are fetched from the `ietf-minutes-data` repository and cached locally in `~/.cache/ietf-llm/{wg_name}/transcript-cache/`.
- **GitHub Auth**: To avoid rate limits when fetching from the API, set the `GITHUB_TOKEN` environment variable.
- **NotebookLM Export**: Use the `--create` flag to automatically create a new notebook in NotebookLM Enterprise and upload all generated archives as sources.

### NotebookLM Export (Enterprise only)

If you have a Google Workspace Enterprise account with NotebookLM enabled, you can programmatically create a notebook and upload your gathered resources.

```bash
ietf-llm httpbis --create [MY_PROJECT_ID]
```

**Requirements:**
1.  **Google Cloud Project**: You must have a GCP project with the **Discovery Engine API** enabled.
2.  **OAuth Credentials**: You need an "OAuth 2.0 Client ID" (Type: Desktop App) from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials).
3.  **Client Secrets**: Save the JSON file as `client_secrets.json` in `~/.config/ietf-llm/` (or specify its path with `--credentials-file`).

The first time you run this, a browser window will open to authorize the application. Your access permissions will be cached in `~/.config/ietf-llm/token.json` (or you can specify with `--token-file`).

## Digest Files

Every run produces three digest files in the destination, designed to be the
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
ietf-llm --update httpbis --embed
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

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.
