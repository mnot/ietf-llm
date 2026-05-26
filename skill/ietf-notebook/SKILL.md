---
name: ietf-notebook
description: Gather and query the public record of an IETF Working Group (charter, drafts, minutes, transcripts, mailing list, GitHub issues) using the `ietf-notebook` CLI. Use when the user asks to gather, refresh, or query materials for a named IETF WG (by shortname like `httpbis`, `quic`, `tls`), or asks substantive questions about a WG's drafts, issues, or list discussion.
---

# ietf-notebook

A capability for working with IETF Working Group corpora gathered by the
[`ietf-notebook`](https://github.com/mnot/ietf-notebook) CLI.

## When to use this skill

Trigger on any of:

- "gather / refresh / update materials for `<wg>`"
- "what's open in `<wg>`?"
- "what does draft `<name>` say about X?"
- "what's the list been saying about Y in `<wg>`?"
- "summarize the state of `<wg>`"

`<wg>` is an IETF working group shortname (e.g. `httpbis`, `quic`, `tls`, `oauth`).

## Gathering

Run the CLI. It persists per-WG config, so destination only needs to be
supplied on first run:

```bash
# First time
ietf-notebook <wg> --destination ~/ietf/<wg> [--github owner/repo ...]

# Subsequent updates (only changed files end up in the destination)
ietf-notebook --update <wg>
```

If the user wants richer digests, add `--summarize` (uses the `llm` package's
configured default model; pass `--summarize-model claude-haiku-4-5` etc. to
override). This is opt-in because it costs API calls.

For semantic search across the corpus, add `--embed` at gather time:

```bash
ietf-notebook --update <wg> --embed [--embed-model 3-small]
```

This builds an embedding index in `~/.cache/ietf-notebook/<wg>/embeddings.db`.
It's incremental — only changed files are re-embedded.

After gathering, the destination directory contains a structured corpus plus
three **digest files** that are your entry points:

- `<wg>-_index.md` — landing page; file inventory by kind, with sizes.
- `<wg>-_issues.md` — one row per GitHub issue (state, title, labels,
  comments, last updated). Sorted open-first.
- `<wg>-_threads.md` — one row per mailing list thread (normalized subject,
  message count, participants, date span).

## Querying — the rules

The corpus is **large**. Mailing-list-per-year files and GitHub issue dumps
are routinely multi-MB. Reading them end-to-end will blow the context window
and produce worse answers, not better ones.

Follow this order strictly:

1. **Always read the three `_*.md` digest files first.** They were generated
   for exactly this purpose. They tell you what exists and where.
2. **For broad / synthesis questions** ("summarize the state of the WG",
   "what are the active controversies"), the digests alone are usually
   enough. Cite issue numbers and thread subjects from them.
3. **For targeted lookups** ("what does §4.2 of draft-X say?"), open just
   that file and read only the relevant section.
4. **For cross-document questions** ("what has the list said about issue
   #42?"), grep first, then read matches in a narrow line window.
5. **Delegate deep dives to a subagent.** If a question genuinely requires
   reading many files or long threads, spawn an Explore or general-purpose
   subagent with a focused prompt and ask it to return a summary. Do not
   pull the raw material into the main context.

### Forbidden by default

- Reading any `*-mailing-list-YYYY.txt` file end-to-end.
- Reading any `*-github-<repo>.txt` file end-to-end.
- `cat`-ing the destination directory.

If a user explicitly insists, warn them about context cost first.

### Good patterns

```bash
# Best: semantic search (if --embed has been run)
ietf-notebook-search <wg> "cookie partitioning concerns" -k 8

# Fallback: locate before reading
grep -nH "cookie partitioning" ~/ietf/httpbis/*.txt

# Narrow read by line range once located
# (use the Read tool's offset/limit, not `cat`)

# Get just the issue header lines from one repo
grep -E "^Issue #|^State:|^Labels:" ~/ietf/httpbis/httpbis-github-*.txt
```

`ietf-notebook-search` returns the top-k chunks (one per mailing list
message, one per GitHub issue, or a windowed slice of a draft) with a
score, source file, chunk index, title, and snippet. After a search hit,
open the source file at the right spot rather than reading it whole.

### MCP server (preferred when available)

If the user has the MCP server registered (`ietf-notebook-mcp`), prefer its
tools over shelling out:

- `list_working_groups()` — what's gathered
- `read_digest(wg, kind)` — index / issues / threads
- `search_corpus(wg, query, k)` — semantic search
- `get_chunk_text(wg, file, chunk_idx)` — full chunk text after a hit
- `read_file_section(wg, file, start_line, max_lines)` — bounded raw read

The MCP tools enforce a hard cap on raw reads (2000 lines per call) so the
context window can't be blown by accident.

## Pointing the user at NotebookLM

For genuinely exploratory, conversational, citation-heavy use across the
whole corpus, the destination directory is designed to be uploaded to
[NotebookLM](https://notebooklm.google.com/). If the user's question is the
kind that wants 20 follow-ups and grounded citations across hundreds of
documents, suggest they upload the directory there rather than asking you to
synthesize it from raw reads.

The CLI also supports `--create <GCP_PROJECT>` to push directly to
NotebookLM Enterprise.

## Common WG shortnames (for disambiguation)

If the user mentions a working group without the shortname, ask. Don't guess.
Common ones: `httpbis` (HTTP), `quic` (QUIC), `tls` (TLS), `oauth` (OAuth),
`core` (Constrained RESTful Environments), `dnsop` (DNS Operations),
`mls` (Messaging Layer Security).
