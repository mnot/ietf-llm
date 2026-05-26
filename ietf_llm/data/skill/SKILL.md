---
name: ietf-llm
description: Query an IETF Working Group's public record (charter, drafts, RFCs, minutes, transcripts, mailing list, GitHub issues) via the `ietf-llm-mcp` MCP server. Use whenever the user asks about a named WG by shortname (`httpbis`, `quic`, `tls`, `aipref`, …) — its state, open issues, draft contents, mailing list discussion, meeting outcomes, or chronology.
---

# ietf-llm

A queryable local corpus of an IETF Working Group's public record,
exposed via `mcp__ietf-llm__*` tools. `<wg>` is always a shortname
(`httpbis`, `aipref`, `tls`, …). If the user names a WG without a
shortname, **ask** — don't guess. If `overview` returns nothing,
the WG hasn't been gathered yet; tell the user to run
`ietf-llm <wg>` from their shell.

## First call: always `overview(wg)`

~30 lines: chairs/ADs, active drafts, top-5 open issues, top-5
recent threads, latest meeting + latest draft. Often enough on its
own. Reading every digest in full burns 80–100 KB for the same
information.

## Catalogue queries: `read_digest(wg, kind, ...filters)`

Always pass filters — unfiltered digests run 15–30 KB; filtered
reads are typically under 2 KB. All filters AND-combine; dates are
ISO (`"2026-05-01"`).

| kind       | rows                                | filters                                                                |
|------------|-------------------------------------|------------------------------------------------------------------------|
| `issues`   | One per GitHub issue                | `state` (`open`/`closed`), `label`, `author`, `limit`                  |
| `threads`  | One per mailing list thread         | `since`, `until`, `min_messages`, `limit`                              |
| `people`   | Participants (chairs/authors lead)  | `role` (e.g. `"Chair"`), `min_messages`, `limit`                       |
| `timeline` | Events in chronological order       | `since`, `until`, `event_kind`, `limit`                                |
| `index`    | File inventory by category          | (none — small)                                                         |

`event_kind` ∈ {`draft-published`, `issue-opened`, `issue-closed`,
`meeting`, `wglc`, `adoption-call`}. `label` / `author` / `role`
are substring matches.

## Substantive questions: `search_corpus(wg, query, k=8)`

Returns top-k chunks with `file`, `chunk_idx`, `line_range`,
snippet, and (for issue chunks) the issue's GitHub `labels`. Pivot
to the source with:

- `get_chunk_text(wg, file, chunk_idx)` — full text of one chunk.
  Pass `end_chunk_idx=N` to fetch a consecutive range (≤20 chunks)
  in one call — use this to read a short thread / issue end-to-end.
- `read_file_section(wg, file, start_line, max_lines)` — bounded
  read for surrounding context or whole-file reading.

`search_corpus` also takes `since` / `until`, `file_pattern` (SQL
LIKE: `"%-thread-%"`, `"%-issue-%"`, `"draft-%"`), and `label`
(substring against an issue's GitHub labels).

**For "arguments for/against X" or "scope debate" questions, try
`label=` first.** The WG's own labels (e.g. `"top-level"`,
`"vocabulary"`, `"ready to close"`) are usually better curation
than semantic ranking alone, and a label-filtered search lands
on the canonical issue immediately instead of via a thread reply
that mentions it.

## File types you'll encounter

- **`<wg>-thread-<date>-<slug>.md`** — one reconstructed mailing
  list conversation. Read in full when the user wants the thread.
- **`<wg>-issue-<owner>-<repo>-<N>.md`** — one GitHub issue with
  full comment history. Same shape as thread files.
- **`draft-…-NN.txt`, `rfc<N>.txt`** — large documents. Use
  `read_file_section` by line range, not whole-file reads.
- **`<name>.pdf.txt`, `*-transcript.md`** — slides and transcripts;
  both carry a meeting-context header at the top so chunks deep
  inside still have attribution.

`list_files(wg)` shows per-file chunk counts so you can bound
`get_chunk_text` ranges without probing.

## Canonical names

Identities are pre-consolidated. "Mark Nottingham" in any digest,
hit, or file is the same actor who appears as `mnot` on GitHub, in
DMARC-rewritten addresses, and via the Datatracker relay. Don't
fabricate identity links — if a GitHub login isn't already mapped
in the `people` digest, say so.

## Anti-patterns

- **Don't read whole digests** when you want a slice — use filters.
- **Don't read `<wg>-mailing-list-YYYY.txt` or
  `<wg>-github-<repo>.txt`** — multi-MB blobs kept only for grep /
  NotebookLM upload. The per-thread and per-issue files cover the
  same content in chunkable form.
- **Don't use `list_files` to find answers** — inventory only.
- **Deep dives** (long thread end-to-end, >5 tool calls): spawn a
  subagent with a tight prompt and have it return a summary.
  Don't pull raw material into the main context.
