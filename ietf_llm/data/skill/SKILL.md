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

IRTF Research Groups work the same way — pass the RG's shortname
(`cfrg`, `hrpc`, `pearg`, …) anywhere `<wg>` appears below. The
tools don't distinguish, and you don't need to either.

## First call: pick by question shape

**Orienting / structural** ("tell me about `<wg>`", "what's this WG
up to?", "who's on it?") → `overview(wg)`. ~30 lines: chairs/ADs,
active drafts, top-5 open issues, top-5 recent threads, latest
meeting + latest draft. Often enough on its own.

**Topical / decision** (specific subject matter or chair rulings)
→ skip `overview` and go straight to:

- _"arguments for/against X"_, _"scope debate"_ →
  `search_corpus(wg, "X", label="...")`. Issue labels
  (`"top-level"`, `"vocabulary"`, `"ready to close"`, …) are the
  WG's own curation; usually better than semantic ranking alone.
  If you don't know the label vocabulary, call `list_labels(wg)`
  first — labels vary by WG and you can't guess them reliably.
- _"what did the WG decide about X?"_, _"position on X"_ →
  `search_corpus(wg, "X", state="closed")`. The chairs' resolution
  lives in closed issues; open threads can be mid-debate noise.
- _"what's open / closed / labelled X?"_, _"who's a chair?"_,
  _"what happened in May?"_ → `read_digest(wg, kind=..., filters)`.
- _"what was said about X?"_ → `search_corpus(wg, "X")` plus
  `get_chunk_text` / `read_file_section` to read hits.

If you're unsure which shape the question is, `overview` is the
safe default — it's cheap and points you at the rest. But for
specific-topic questions, calling `overview` first wastes a turn.

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
`meeting`, `poll`, `wglc`, `adoption-call`, `charter-approved`,
`chair-appointed`, `group-state`, `doc-adopted`, `doc-iesg`,
`doc-rfc`, `doc-wglc`}. The Datatracker-sourced group (charter,
chair, doc-*) spans the WG's full history regardless of the
`--months` window; charter approvals and chair appointments are
always included. `poll` events point at cached
`<wg>-polls-<meeting>-<datetime>.md` files — session polls aren't
formal consensus but signal where a session was leaning.
`label` / `author` / `role`
are substring matches.

## Substantive questions: `search_corpus(wg, query, k=8)`

Returns top-k chunks with `file`, `chunk_idx`, `line_range`,
snippet, and (for issue chunks) the issue's GitHub `labels` plus
open/closed `state`. Pivot to the source with:

- `get_chunk_text(wg, file, chunk_idx)` — full text of one chunk.
  Pass `end_chunk_idx=N` to fetch a consecutive range (≤20 chunks)
  in one call — use this to read a short thread / issue end-to-end.
- `read_file_section(wg, file, start_line, max_lines)` — bounded
  read for surrounding context or whole-file reading.

`search_corpus` also takes `since` / `until`, `file_pattern` (SQL
LIKE: `"%-thread-%"`, `"%-issue-%"`, `"draft-%"`), `label`
(substring against an issue's GitHub labels), and `state`
(`"open"` / `"closed"`).

**For "arguments for/against X" or "scope debate" questions, try
`label=` first.** The WG's own labels (e.g. `"top-level"`,
`"vocabulary"`, `"ready to close"`) are usually better curation
than semantic ranking alone, and a label-filtered search lands
on the canonical issue immediately instead of via a thread reply
that mentions it.

**For "what did the WG decide about X?"-shaped questions, add
`state="closed"`.** The chairs' resolution lives in the closed
issue; older mid-debate threads can be misleading once a decision
has landed.

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
- **`<wg>-polls-<meeting>-<datetime>.md`** — session poll records
  from Datatracker. Read in full when answering "where was the
  room leaning on X?" — polls aren't consensus but they're signal.

`list_files(wg)` shows per-file chunk counts so you can bound
`get_chunk_text` ranges without probing.

## Reading a debate in chronological order

`search_corpus` ranks by relevance, which can hide whether an
argument is an early objection or a settled position. Add
`sort="date"` to re-order the top-k hits oldest-first instead — a
consumer reading the result top-to-bottom sees how the debate
evolved. Scope to one issue with `file_pattern="%-issue-…-N.md"`
or to a time window with `since` / `until`.

If you want the *whole* issue end-to-end (not just hits matching a
query), read the per-issue file directly via `read_file_section` or
`get_chunk_text(..., end_chunk_idx=N)` — the file is already in
chronological order with an outline of who spoke when.

## Canonical names

Identities are pre-consolidated. "Mark Nottingham" in any digest,
hit, or file is the same actor who appears as `mnot` on GitHub, in
DMARC-rewritten addresses, and via the Datatracker relay. Don't
fabricate identity links — if a GitHub login isn't already mapped
in the `people` digest, say so.

## How the IETF works

A few interpretive norms that shape how to read the corpus:

- **Individuals, not employers.** People participate as
  individuals, not as representatives of their company. Don't
  attribute a position to an employer based on the author's email
  domain or affiliation. Only treat something as a company
  position when the author explicitly frames it that way
  ("my company…", "speaking for X…", "as an employee of Y…").

- **Decisions happen on the mailing list, not in meetings.** A
  meeting might map out a proposal; the binding move is
  confirmation on the list. When the user asks "what did the WG
  decide", look at chair statements and closed-issue resolutions
  that reference list discussion, not meeting minutes alone. A
  proposal that "got agreement in the room" isn't a decision
  until it's confirmed on list.

- **Consensus is chair-declared, not vote-counted.** Only the
  chairs declare consensus, and they weigh argument substance,
  not headcounts. A session poll showing 28-4 isn't a decision —
  it's a tool the chairs use to gauge the room. Report polls and
  raise-of-hands as *signal*, not outcomes; report chair
  declarations on list as outcomes.

These norms apply equally to IRTF Research Groups.

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
