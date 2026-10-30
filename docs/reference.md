# Command & gather reference

**This document is for:** the `ietf-llm` command list and every gather flag. Back to the
[docs index](README.md).

## Commands

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | Gather / refresh a corpus | network | cache |
| `ietf-llm-export` | Mirror cache to dir, or push to NotebookLM Enterprise | cache | dir / NotebookLM |
| `ietf-llm-search` | Semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | Expose the cache to MCP clients | cache | stdio / HTTP (MCP) |

The commands are independent. The cache (`~/.cache/ietf-llm/<name>/`) is what `ietf-llm-export`,
`ietf-llm-search` and `ietf-llm-mcp` read.

## Gather options

```bash
ietf-llm [OPTIONS] <name>
```

`<name>` is the corpus to gather, classified automatically:

- a **Working Group / Research Group / editorial WG / BoF** shortname (`httpbis`, `cfrg`, `rswg`),
  gathered in full (charter, drafts, meetings, ballots, mailing list);
- a **mailing list** archived at mailarchive.ietf.org (`last-call`, `irtf-discuss`,
  `rfc-interest`), gathered on its own;
- any other **label** given explicit sources (`--draft`, `--mailing-list`, `--github`,
  `--new-drafts`, `--author`);
- an `x-` prefix, which skips the Datatracker group lookup (a fully manual corpus).

A name that is none of these and has no configured sources is rejected as a likely typo.

**Sources** (all persisted, so later runs pick them up):

- `--github OWNER/REPO` — a GitHub repo whose issues and pull requests to include. A WG's active
  draft repos are discovered on the first gather; use this to override or extend them.
- `--draft DRAFT-NAME` — an extra Internet-Draft to track. The version suffix is stripped and every
  revision is gathered.
- `--mailing-list LIST` — an extra list to sync, as a bare name or a full address (`rswg`,
  `rswg@rfc-editor.org`); the domain is ignored.
- `--new-drafts` — subscribe to *new* Internet-Drafts: every `-00` submitted within `--months`.
  Drafts age out of the rolling window.
- `--author PERSON` — every draft `PERSON` authored: an email (`mnot@mnot.net`, recommended), a
  Datatracker person id, or an exact full name. Drafts only.
- `--add-mentioned-drafts` — also pull drafts that the corpus's threads and issues mention. Once
  added, a draft stays.
- `--include-related-drafts` — also gather active `draft-<author>-<wg>-<topic>` drafts the WG
  follows but hasn't adopted. Off by default; can be large for popular WGs.
- `--rfcs` — also download the WG's published RFCs into `drafts/` and index them. Off by default,
  because the RFC tools already cover the whole series.

**Scope and filtering:**

- `--months N` — months of mailing list, meeting and new-draft history (default 12). `0` means all
  history and requires `--force`.
- `--github-label LABEL` / `--exclude-github-label LABEL` — include or exclude issues by label.
- `--force` — gather even if the corpus was gathered within the last 6 hours
  (`IETF_LLM_GATHER_MIN_INTERVAL` sets the window, in hours).

**Digests and search index** (all but `--rebuild-embeddings` are
[global settings](models.md#global-settings)):

- `--summarize` / `--summarize-model MODEL` — add LLM-generated one-liners to digests.
- `--no-embed` / `--embed` — skip, or restore, the semantic search index that backs
  `ietf-llm-search` and the MCP `search_corpus` tool. The index is on by default and incremental;
  `grep_corpus` reads the gathered files, so it works without it.
- `--embed-model MODEL` — embedding model id: the default on-device model, or an `openai-embed/...`
  id for a remote endpoint.
- `--rebuild-embeddings` — drop and re-embed everything instead of updating incrementally.

**Seeding** (see the [seed store](seed-store.md)):

- `--no-seed` / `--seed` — turn seeding from the public seed store off or on. The choice persists
  across gathers.
- `--refresh-base` — re-pull the seed snapshot even when a local copy exists, then freshen.

**Disk use:**

- `--no-raw` — skip the regenerable `raw/` text dumps. On automatically for MCP gathers and the
  cloud backend.
- `--no-pdf` — delete slide-deck PDFs after extracting their text. On automatically for MCP gathers
  and the cloud backend.

**Cache, config and housekeeping:**

- `--list` — list cached corpora (name, kind, status, last-gathered, one-line subject), then exit.
- `--all` — refresh every gathered corpus the store knows about, each with its own persisted
  config. Takes no NAME, and refuses `--clear-config` and `--clear-cache`. A failing corpus doesn't
  stop the sweep, but the exit status is non-zero. On the cloud backend this is the whole fleet; see
  [Storage & locations](storage.md#the-cloud-backend).
- `--all --used-within DAYS` — only corpora read within the last DAYS days through the MCP read
  tools. A corpus with no recorded access counts from its last-gathered time. Set
  `IETF_LLM_RECORD_ACCESS=off` on the read deployment to stop recording access.
- `--clear-cache` — wipe everything cached for this corpus and re-download: its gathered files and
  index, its cached mailing-list mail, and on the cloud backend the copies of that mail and of the
  Datatracker ETag store in the bucket. Other corpora and shared caches are untouched. Mail older
  than `--months` can't be downloaded again, so it is lost for good. If the bucket can't be cleared,
  the gather fails.
- `--clear-config` — clear this corpus's persisted gather and export config.
- `--discover-github NAME` — print the GitHub repos recommended for a WG and the matching `--github`
  flags, then exit. Gathers nothing and writes no config.
- `--init` — set the machine up without gathering: see [the RFC series](gathering.md#the-rfc-series-which-no-gather-brings).
- `--install-skills` — install the bundled IETF skills into each supported agent harness found
  (Claude Code, Codex, Gemini CLI, opencode), overwriting existing copies, then exit.
- `--completion SHELL` — print a completion script; see [shell completion](shell-completion.md).
- `--quiet` / `-q` — print only errors; `--verbose` / `-v` — detailed progress.

Per-corpus settings live in `~/.config/ietf-llm/<name>/gather.json`, and tool-wide settings in
`~/.config/ietf-llm/config.json`. To move any of these directories, see
[Storage & locations](storage.md).
