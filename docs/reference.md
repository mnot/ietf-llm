# Command & gather reference

**This document is for:** the full `ietf-llm` command list and every gather flag. — Back to the
[docs index](README.md).

## Commands

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | Gather / refresh a corpus | network | cache |
| `ietf-llm-export` | Mirror cache to dir, or push to NotebookLM Enterprise | cache | dir / NotebookLM |
| `ietf-llm-search` | Semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | Expose the cache to MCP clients | cache | stdio / HTTP (MCP) |

All four are independent. The cache (`~/.cache/ietf-llm/<name>/`) is the single source of truth;
everything else reads from it.

## Gather options

```bash
ietf-llm [OPTIONS] <name>
```

`<name>` is the corpus to gather, classified automatically:

- a **Working Group / Research Group / editorial WG / BoF** shortname (`httpbis`, `cfrg`, `rswg`) —
  gathered in full (charter, drafts, meetings, ballots, mailing list);
- a **mailing list** archived at mailarchive.ietf.org — IETF, IRTF, or RFC-Editor (`last-call`,
  `irtf-discuss`, `rfc-interest`) — that list on its own;
- any other **label** given explicit sources (`--draft` / `--mailing-list` / `--github` /
  `--new-drafts` / `--author`);
- prefix with `x-` to skip the Datatracker group lookup entirely (a fully manual corpus).

A name that is none of these and has no configured sources is rejected as a likely typo.

**Sources**:

- `--github OWNER/REPO` — a GitHub repo whose issues to include. _Working Groups usually
  don't need this_: the first gather auto-discovers the group's active draft repos based upon Datatracker. Use `--github` to override or extend that set.
- `--draft DRAFT-NAME` — an extra Internet-Draft to track, beyond a WG's own documents. Version
  suffix stripped; every revision gathered.
- `--mailing-list LIST` — an extra list to sync (any archived at mailarchive.ietf.org). A bare name
  or a full address; the domain is optional and ignored (`rswg`, `rswg@rfc-editor.org`).
- `--new-drafts` — subscribe to *new* Internet-Drafts: every `-00` submitted within `--months`
  (rolling window; drafts age out).
- `--author PERSON` — every draft `PERSON` authored. `PERSON` is an email (`mnot@mnot.net`,
  recommended), a Datatracker person id, or an exact full name. Drafts only.
- `--add-mentioned-drafts` — also pull drafts the corpus's threads/issues mention but don't already
  include. Sticky.

**Scope & filtering:**

- `--months N` — months of mailing list / meeting / new-draft history (default 12).
- `--github-label LABEL` / `--exclude-github-label LABEL` — include / exclude issues by label.

**Digests & search index:**

- `--summarize` / `--summarize-model MODEL` — add LLM-generated one-liners to digests via the `llm`
  package.
- `--no-embed` — skip the semantic search index (it backs `ietf-llm-search` and the MCP
  `search_corpus` tool). On by default, incremental. The MCP `grep_corpus` tool reads the gathered
  files rather than the index, so exact-string search still works without it.
- `--embed-model MODEL` — embedding model id (default: a small local model; or an `openai-embed/...`
  id for a remote endpoint).
- `--rebuild-embeddings` — drop and re-embed everything instead of the incremental update.

`--summarize`, `--summarize-model`, `--embed-model`, and `--no-embed` are **global** settings, not
per-corpus: set once, applied everywhere, and overridable by environment. See
[Model backends](models.md#global-settings).

**Cache & config:**

- `--list` — list cached corpora (name, kind, status, last-gathered, and a one-line subject — the
  group name, list, or tracked author), then exit.
- `--all` — refresh every gathered corpus the configured store knows about, each with its own
  persisted config (no positional NAME; `--clear-config` is refused). On the cloud backend this is
  the whole fleet, not just this host's local cache — see [Storage &
  locations](storage.md#the-cloud-backend).
- `--all --used-within DAYS` — restrict `--all` to corpora **read within the last DAYS days** (via
  the MCP read tools). A corpus with no recorded access falls back to its last-gathered time, so a
  freshly gathered corpus that hasn't been read yet still gets a grace period rather than being
  skipped. Set `IETF_LLM_RECORD_ACCESS=off` on the read deployment to disable access recording
  entirely (the filter then sees only gather times).
- `--clear-cache` — wipe this corpus's cache and re-download.
- `--clear-config` — clear this corpus's persisted config.
- `--discover-github NAME` — print the GitHub repos discovery recommends tracking for a WG (those
  with Internet-Draft sources and an active issue tracker), and the matching `--github` flags, then
  exit. A dry run — gathers nothing, writes no config.
- `--quiet` / `--verbose`.

Per-corpus settings live in `~/.config/ietf-llm/<name>/gather.json`; tool-wide settings live in
`~/.config/ietf-llm/config.json`. To put any of these directories elsewhere, see
[Storage & locations](storage.md).

