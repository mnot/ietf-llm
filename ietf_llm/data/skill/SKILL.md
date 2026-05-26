---
name: ietf-llm
description: Query an IETF Working Group's public record (charter, drafts, RFCs, minutes, transcripts, mailing list, GitHub issues) via the `ietf-llm-mcp` MCP server. Use whenever the user asks about a named WG by shortname (`httpbis`, `quic`, `tls`, `aipref`, …) — its current state, open issues, draft contents, mailing list discussion, meeting outcomes, or chronology. Also use when the user asks to gather or refresh materials for a WG.
---

# ietf-llm

You have access to a local, queryable corpus of an IETF Working Group's
public record. The tools live under `mcp__ietf-llm__*` and are backed
by the `ietf-llm` package, which gathers WG material into
`~/.cache/ietf-llm/<wg>/`.

`<wg>` is always a WG shortname (e.g. `httpbis`, `aipref`, `tls`).
If the user mentions a WG without giving a shortname, **ask** — don't
guess.

## The opening move: always `overview` first

For any new question about a WG, your first tool call should be:

```
overview(wg)
```

It returns a ~30-line summary: chairs and Area Director, active drafts
with their authors, the five most recently updated open issues, the
five most recent mailing list threads, and the latest meeting plus
latest draft publication. This is **2–3 KB**, vs the **80–100 KB**
you'd burn reading every digest in full. It's enough to answer the
common "tell me about this WG" question by itself.

Only descend into the specialised digests after the overview tells you
where to look.

## The specialised digests

Five kinds, all read via `read_digest(wg, kind, ...filters)`:

| kind       | What it's for                                       |
|------------|-----------------------------------------------------|
| `index`    | File inventory by category (charter, drafts, etc.)  |
| `issues`   | One row per GitHub issue — state, labels, author    |
| `threads`  | One row per mailing list thread, linked to its file |
| `people`   | Participants; leads with chairs/ADs and authors     |
| `timeline` | Chronological event log (drafts published, issues, meetings, WGLCs) |

### **Always use filters for catalogue queries**

The full digests can be 15–30 KB each. Filtered reads are typically
under 2 KB. Always pass filters when answering catalogue questions:

```
# "What's open?" → ~1 KB, not 15 KB
read_digest("aipref", "issues", state="open", limit=10)

# "What discussed about RAG?" → only issues with that label
read_digest("aipref", "issues", label="rag")

# "What's been happening recently?" → newest 20 threads
read_digest("aipref", "threads", limit=20)

# "Who's a chair?"
read_digest("aipref", "people", role="Chair")

# "When did WGLC happen?"
read_digest("aipref", "timeline", event_kind="wglc")

# "What happened in May 2026?"
read_digest("aipref", "timeline", since="2026-05-01", until="2026-05-31")
```

Filter parameters by kind:

- **issues**: `state` (`"open"` / `"closed"`), `label` (substring), `author` (substring), `limit`
- **threads**: `since`, `until` (ISO date), `min_messages`, `limit`
- **people**: `role` (substring, e.g. `"Chair"`), `min_messages`, `limit`
- **timeline**: `since`, `until`, `event_kind` (`draft-published` / `issue-opened` / `issue-closed` / `meeting` / `wglc` / `adoption-call`), `limit`

## Semantic search

For substantive questions ("what was said about X?", "what's the WG's
stance on Y?"), use `search_corpus`:

```
search_corpus(wg, "vocabulary scope debate", k=8)
```

Returns top-k chunks with file, chunk index, line range, and a snippet.
Each chunk's `file` field points at a real cache file — pivot to it
with `get_chunk_text(wg, file, chunk_idx)` for the full chunk or
`read_file_section(wg, file, start_line, max_lines)` for surrounding
context. To read a short thread / issue end-to-end in one call, pass
`get_chunk_text(..., end_chunk_idx=N)` to fetch a consecutive range
(capped at 20 chunks). `list_files(wg)` shows per-file chunk counts
so you can bound the range without probing.

`search_corpus` also takes filters:

- `file_pattern` — SQL LIKE pattern (e.g. `"%-thread-%"` for mailing
  list only, `"%-issue-%"` for GitHub issues only, `"draft-%"` for drafts)
- `since` / `until` — ISO dates; restricts to dated chunks
- `k` — number of hits (default 10)

## Reading the corpus

The cache contains four kinds of files an agent will routinely encounter:

- **Per-thread mailing list files** (`<wg>-thread-<date>-<slug>.md`) —
  one file per reconstructed conversation, with a header, an outline,
  and one section per message. **Read these in full** when a search
  hit lands inside one and the user wants the whole conversation.
- **Drafts** (`draft-…-NN.txt`, `rfc<N>.txt`) — self-contained
  documents with their own author/date headers. Use
  `read_file_section` with a line range; don't read whole drafts.
- **Per-issue GitHub files** (`<wg>-issue-<owner>-<repo>-<NNN>.md`) —
  one file per reconstructed issue, with frontmatter, outline, and
  one section per comment. Same shape as the per-thread files; read
  in full when an issue is the answer. The legacy
  `<wg>-github-<owner>-<repo>.txt` blob is kept only for grep /
  NotebookLM upload and is NOT in the search index.
- **Slide / transcript markdown** (`<name>.pdf.txt`,
  `*-transcript.md`) — both carry a meeting-context header at the
  top so chunks deep inside still have attribution.

Every chunk in the search index, and every search hit, refers back
to one of these file types.

## Canonical names

Identities are pre-consolidated. When you see "Mark Nottingham" in any
digest, search snippet, or thread file, that's the canonical name —
the same person also appears as `mnot` on GitHub, in DMARC-rewritten
addresses, and via the Datatracker relay. Don't go looking for
multiple actors; `<wg>-_people.md` is the lookup table when you need
to verify the link.

## Anti-patterns — never do these

- **Don't read whole digests when you just want a slice.** Use filters.
- **Don't read whole `<wg>-mailing-list-YYYY.txt` files** — they're
  multi-MB per year and exist only for human grep / NotebookLM upload.
  The per-thread files are the readable form.
- **Don't read whole `<wg>-github-<repo>.txt` files** — multi-MB.
  Use the issues digest (filtered) or search.
- **Don't list files to scan content** — `list_files` is for inventory,
  not for finding answers.
- **Don't fabricate identity links.** If the GitHub login doesn't
  appear under a Person in `<wg>-_people.md`, you don't know who it
  is. Say so.

## Deep dives: delegate to a subagent

If answering takes more than ~5 tool calls or requires reading a long
thread end-to-end, spawn a focused subagent with a tight prompt and
have it return a summary. Don't pull raw material into the main
context.

## Gathering (when the user asks to refresh)

The CLI populates the cache. Per-WG config persists, so subsequent
runs need only the shortname:

```bash
# First time — gather + build embedding index
ietf-llm <wg> [--github owner/repo ...] --embed

# Refresh (config remembered)
ietf-llm <wg>

# Refresh every gathered WG at once
ietf-llm --all
```

`--embed` is required for `search_corpus` to work — it builds the
local embedding index using a small sentence-transformers model
(no API key, ~130 MB one-time download).

Add `--summarize` for LLM-generated one-line summaries in the digests
(needs `llm` configured with a provider key).

After gathering, the MCP server picks up the new state automatically
on its next tool call — nothing to restart.

## NotebookLM export

If the user wants conversational exploration with grounded citations
across the whole corpus, suggest NotebookLM:

```bash
ietf-llm-export <wg> --destination ~/ietf/<wg>
# or
ietf-llm-export <wg> --create <GCP_PROJECT>     # Enterprise
```

Don't conflate this with gathering — `ietf-llm` no longer accepts
`--destination` or `--create`.

## Quick reference: tool call patterns by question shape

| Question                        | First tool call                                            |
|---------------------------------|------------------------------------------------------------|
| "Tell me about `<wg>`"          | `overview(wg)`                                             |
| "What's open right now?"        | `read_digest(wg, "issues", state="open", limit=10)`        |
| "Recent mailing list activity?" | `read_digest(wg, "threads", limit=10)`                     |
| "When did X happen?"            | `read_digest(wg, "timeline", event_kind="…")`              |
| "Who are the chairs?"           | `read_digest(wg, "people", role="Chair")`                  |
| "What was said about X?"        | `search_corpus(wg, "X", k=8)`                              |
| "Read thread Y in full"         | `read_file_section(wg, "<wg>-thread-…md", 1, 2000)`        |
| "What did slide deck Z cover?"  | `read_file_section(wg, "<deck>.pdf.txt", 1, 2000)`         |
