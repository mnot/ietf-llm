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

Tools are lazy-loaded by the harness on first `tool_search` match,
but stay loaded for the rest of the session — discover incrementally
rather than batching every possible name into one query. The
discovery tools (`list_labels`, `list_files`, `list_working_groups`)
print concrete next-call signatures in their output, so reading
their response is usually enough to know what to fetch next.

## First call: pick by question shape

**Orienting / structural** ("tell me about `<wg>`", "what's this WG
up to?", "who's on it?") → `overview(wg)`. ~30 lines: chairs/ADs,
active drafts, top-5 open issues, top-5 recent threads, latest
meeting + latest draft. Often enough on its own.

**Topical / decision** (specific subject matter or chair rulings)
→ usually go straight to the tool below. `overview` is also cheap
(~30 lines) and can still help you pick a better search query when
the topic is unfamiliar — the active drafts, chair list, and
recent-threads section often surface terms worth searching for.

- _"arguments for/against X"_, _"scope debate"_ →
  `read_digest(wg, kind="issues", label="...", include_bodies=True)`.
  This returns the catalogue PLUS each issue's opening description
  in one call — usually enough to answer the question without
  follow-up file reads. `list_labels(wg)` first if you don't know
  the vocabulary. `search_corpus(wg, "X", label="...")` is a
  back-up for when you want semantic ranking inside the cluster
  (e.g. "the part of these issues that argues Y"), but for
  *coverage* — every distinct argument across the cluster — the
  `include_bodies` digest is the right primitive.
- _"what did the WG decide about X?"_, _"position on X"_ →
  `search_corpus(wg, "X", state="closed")`. The chairs' resolution
  lives in closed issues; open threads can be mid-debate noise.
- _"what's open / closed / labelled X?"_, _"who's a chair?"_,
  _"what happened in May?"_ → `read_digest(wg, kind=..., filters)`.
- _"what was said about X?"_ → `search_corpus(wg, "X")` plus
  `get_chunk_text` / `read_file_section` to read hits.

If you're unsure which shape the question is, `overview` is the
safe default — it's cheap and points you at the rest.

## Catalogue queries: `read_digest(wg, kind, ...filters)`

Always pass filters — unfiltered digests run 15–30 KB; filtered
reads are typically under 2 KB. All filters AND-combine; dates are
ISO (`"2026-05-01"`).

| kind       | rows                                | filters                                                                |
|------------|-------------------------------------|------------------------------------------------------------------------|
| `issues`   | One per GitHub issue                | `state` (`open`/`closed`), `label`, `author`, `limit`, `include_bodies`|
| `threads`  | One per mailing list thread         | `since`, `until`, `min_messages`, `subject` (substring), `limit`       |
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
snippet, and — for issue chunks — the issue's GitHub `labels`,
open/closed `state`, `duplicate of: #N` when marked, and a
`closing: …` preview when the issue is closed. The snippet ends
with `[truncated]` when the chunk has more content than what's
shown; absence of the marker means the snippet is the whole chunk.
Pivot to the source with:

- `get_chunk_text(wg, file, chunk_idx)` — full text of one chunk.
  Pass `end_chunk_idx=N` to fetch a consecutive range (≤20 chunks)
  in one call — use this to read a short thread / issue end-to-end.
- `get_chunks_batch(wg, [{file, chunk_idx, end_chunk_idx?}, …])` —
  the same, but across multiple files in one round-trip. Use when
  search hits span several files and you want all of them.
- `read_file_section(wg, file, start_line, max_lines)` — bounded
  read for surrounding context or whole-file reading.
- `fetch_by_url(wg, url)` — when the user pastes a GitHub issue
  URL or an IETF mail-archive permalink, this resolves it to the
  cached chunk content without you needing to know the file name.
  Also reach for it when a chunk you're reading **cites** a URL
  inline (a quoted message link, a referenced issue) and you want
  the underlying content rather than the cited paraphrase —
  cheaper than searching, and the citation isn't always faithful.

`search_corpus` also takes `since` / `until`, `file_pattern` (SQL
LIKE on the file's relative path under the WG cache: `"threads/%"`,
`"issues/%"`, `"meetings/%"`, `"drafts/%"`), `label` (substring
against an issue's GitHub labels), `state` (`"open"` / `"closed"`),
and `group_by="file"`.

**`group_by="file"`** collapses the per-chunk hit list to one row
per file with a hit count. Use this for **breadth** questions —
*"which threads discuss the MLKEM controversy?"* returns four
distinct threads instead of fifteen overlapping chunks, often
saving four follow-up searches. Switch back to the default
per-chunk view for **depth** questions — *"what did Alice say
about Y?"* — where you want the actual quotes.

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

All paths are relative to the WG's cache root (`<wg>/files/`).

- **`threads/<date>-<slug>.md`** — one reconstructed mailing list
  conversation. Read in full when the user wants the thread.
- **`issues/<owner>-<repo>/<N>.md`** — one GitHub issue with
  full comment history. Same shape as thread files. Frontmatter
  includes `**Duplicate of:** #N` when a comment calls out a
  duplicate, and `**Closing rationale:**` (last comment) when the
  issue is closed — both are load-bearing for "what did the WG
  decide" questions.
- **`drafts/draft-…-NN.txt`, `drafts/rfc<N>.txt`** — large
  documents. Use `read_file_section` by line range, not whole-file
  reads.
- **`meetings/<code>/minutes.md`** — meeting minutes, e.g.
  `meetings/ietf125/minutes.md` or
  `meetings/interim2026aipref01/minutes.md`.
- **`meetings/<code>/slides/<name>.pdf.txt`** — extracted slide
  text. The `.pdf.txt` carries a meeting-context header so chunks
  deep inside still have attribution.
- **`meetings/<code>/transcripts/<YYYYMMDDHHmm>.md`** — session
  transcripts (with meeting-context header).
- **`meetings/<code>/polls/<YYYYMMDDHHmm>.md`** — session poll
  records from Datatracker. Read in full when answering "where was
  the room leaning on X?" — polls aren't consensus but they're
  signal.
- **`digests/<kind>.md`** — the catalogue digests (`index`,
  `issues`, `threads`, `people`, `timeline`). Read via `read_digest`,
  not `get_chunk_text`.

`list_files(wg)` shows per-file chunk counts so you can bound
`get_chunk_text` ranges without probing.

## Reading a debate in chronological order

**For "how did this evolve / arc of the debate" questions, reach
for `sort="date"` first** — not as a niche option. Relevance ranking
mixes early objections with late settled positions, which hides the
*direction* of the debate; sorted-by-date reads top-to-bottom as a
narrative. Scope to one issue with `file_pattern="issues/…/N.md"`
or to a time window with `since` / `until`.

For "where does the WG stand right now on X" questions, start with
the chair's most recent statement before reconstructing the arc.
Try `read_digest(wg, "issues", state="closed", label="X")` for
chair-resolved decisions, or
`search_corpus(wg, "X consensus|resolution|wglc", sort="date")` and
scan the latest hits whose chunk title carries a `(Chair)` role tag —
those are usually the load-bearing posts.

If you want the *whole* issue or thread end-to-end (not just hits
matching a query), use `read_file_section(wg, file, start_line=1)`
on the per-issue / per-thread file — it's already in chronological
order with an outline of who spoke when, and the line cap is high
enough to cover virtually every issue in one call. Reach for
`get_chunks_batch` only when you need chunks from *multiple* files
in one round-trip.

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
- **Don't read anything under `raw/`** — multi-MB per-year mailing-
  list dumps and legacy GitHub text blobs, kept only for grep /
  NotebookLM upload. Same content lives in the per-thread
  (`threads/`) and per-issue (`issues/`) files in chunkable form;
  `list_files` tags these as `(not indexed)` for the same reason.
- **Don't use `list_files` to find answers** — inventory only.
- **Deep dives** (long thread end-to-end, >5 tool calls): spawn a
  subagent with a tight prompt and have it return a summary.
  Don't pull raw material into the main context.
