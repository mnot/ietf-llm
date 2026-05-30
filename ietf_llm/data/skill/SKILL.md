---
name: ietf-llm
description: Query the gathered public record of an IETF effort — a Working Group / Research Group, a mailing list (e.g. `last-call`), or a set of Internet-Drafts — via the `ietf-llm-mcp` MCP server (charter, drafts, RFCs, minutes, transcripts, mailing list, GitHub issues). Use whenever the user asks about a named corpus by shortname (`httpbis`, `quic`, `tls`, `cfrg`, …) — its state, open issues, draft contents, mailing list discussion, meeting outcomes, or chronology.
---

# ietf-llm

A queryable local record of an IETF effort — a Working Group, a
mailing list, a set of drafts — exposed via `mcp__ietf-llm__*` tools.
`<corpus>` is the corpus name: often a WG / RG shortname (`httpbis`,
`tls`, `cfrg`), but see the kinds below. If the user names something
and you can't tell which corpus they mean, **ask** — don't guess.

**Default to this corpus; don't reflexively crawl IETF sites.**
If a WG isn't here (`overview` returns nothing, or it's missing
from `list_corpora`), the usual answer is NOT to go scrape
the data yourself — it's to tell the user to gather it: `ietf-llm
<corpus>` from their shell (e.g. `ietf-llm httpbis`). They can see
what's already cached with `ietf-llm --list`.

Reaching out to a live IETF resource (datatracker.ietf.org,
mailarchive.ietf.org, a draft URL, GitHub) is occasionally
necessary — e.g. to confirm a draft's *current* state, which the
snapshot can't know. When you do, **tell the user you're going
outside the corpus, and flag it if you're making more than a
couple of requests** so they can decide whether to re-gather
instead. Prefer the corpus for anything it can answer.

Not every corpus is a Working Group. `list_corpora` tags each
one with a **kind**: `group` (a WG / IRTF RG / editorial WG / BoF —
shortname convention `cfrg`, `hrpc`, …, with a `status` of `active` /
`concluded` / `bof`), `list` (a standalone mailing list like
`last-call`), `custom` (an explicit draft / repo set), or `synthetic`
(an `x-` corpus, e.g. `x-webbotauth` — drafts/lists with no formal WG,
hence no charter, leadership, or Datatracker timeline). **Every tool
takes any kind** — the `corpus` argument is the corpus name, not
specifically a WG.

**Loading the tools.** Every tool is named `ietf-llm__…`. If your
client loads MCP tools lazily — you have to search for a tool before
you can call it — load the core set in one search rather than
discovering them one at a time: `overview`, `read_digest`,
`search_corpus`, `read_topic`, `tally_positions`, `find_replies`,
`find_citations`, `list_corpora`, `list_labels`, `list_files`.

## First call: pick by question shape

**Orienting / structural** ("tell me about `<corpus>`", "what's this WG
up to?", "who's on it?") → `overview(corpus)`. ~30 lines: chairs/ADs,
status + area, charter excerpt, key resources (repo / home page /
chat), Internet-Drafts (RFCs collapsed), top-5 open issues, top-5
recent threads, and the last ~10 timeline events (ballots, WGLCs,
adoption calls, meetings, publications). Often enough on its own.

**Topical / decision** (specific subject matter or chair rulings)
→ go straight to the tool below. `overview` is still cheap if the
topic is unfamiliar and you want terms to search for.

- _"arguments for/against X"_, _"scope debate"_ →
  `read_digest(corpus, kind="issues", label="...", include_bodies=True)`
  — catalogue PLUS each issue's opening description in one call,
  usually enough without follow-up reads. `list_labels(corpus)` first
  if you don't know the vocabulary. For *coverage* (every distinct
  argument) the `include_bodies` digest beats semantic search;
  reach for `search_corpus(corpus, "X", label="...")` only when you
  want ranking *inside* the cluster.
- _"what did the WG decide about X?"_, _"position on X"_ →
  `search_corpus(corpus, "X", state="closed")`. The chairs' resolution
  lives in closed issues; open threads can be mid-debate noise.
- _"what's open / closed / labelled X?"_, _"who's a chair?"_,
  _"what happened in May?"_ → `read_digest(corpus, kind=..., filters)`.
- _"what was said about X?"_ → `search_corpus(corpus, "X")` plus
  `get_chunk_text` / `read_file_section` to read hits.
- _"how did the debate on X evolve?"_, _"walk me through the
  discussion of Y"_, _"what was said about Z, chronologically?"_
  → `read_topic(corpus, "X")`. Returns full messages (not snippets) in
  date order across threads and issues. Add `include_replies=True`
  when you want sub-thread descendants pulled in even if they don't
  themselves match the query.

  **If `read_topic` returns a thread that looks like a chair poll**
  ("please reply indicating which option you prefer", numbered
  options, terse one-word replies), follow up with
  `tally_positions(corpus, "<file>")` on that thread. The poll-syntax
  detection bucket'll show the option counts the consumer is
  actually asking about.
- _"what other threads cover this topic?"_, _"what's the broader
  context for thread X?"_ → `list_labels(corpus)` shows the WG's
  `[xxx]`-style subject prefixes; then
  `read_digest(corpus, kind="threads", subject="[mlkem]")` returns
  every thread in that cluster. Multiple parallel threads on one
  topic are common (e.g. "WebBotAuth Direction" + "Reframing the
  Direction" + "Architectural Limitations" in one week) — the
  cluster filter pulls them together.
- _"read the whole thread X end-to-end"_, _"give me thread X in
  full"_ (no query, just one file) → `read_file_section(corpus,
  "threads/<file>.md", start_line=1)`. The per-thread file is
  already in chronological order with an outline header; the
  5000-line cap covers virtually every thread in one call. Reach
  for this when you want **the file**, not query-anchored hits —
  `read_topic` requires a query and will drop messages that don't
  match it.
- _"what threads engage with draft X?"_, _"who's been discussing
  draft Y?"_, _"is this draft actually being talked about?"_ →
  `find_citations(corpus, "draft-...")`. Returns every thread / issue
  file that mentions the draft, with chunk indices and short
  context excerpts. The `overview` Internet-Drafts section also shows
  the citation count inline (`cited in N`) when it's non-zero.
  Useful in both directions: from a draft to the discussion, and
  from a thread mention to the wider conversation.
- _"what does §N of draft X say?"_, _"quote the Security
  Considerations of draft Y"_, _"what's actually in the draft?"_
  → `read_file_section(corpus, "drafts/<draft-name>-NN.txt",
  start_line=1)`. **When the question is about the literal text of
  a draft, read the draft.** Don't reconstruct it from what people
  said about it on the list — the document is the artefact, the
  list traffic is commentary. `list_files(corpus, pattern="drafts/*")`
  shows what's cached.
- _"what's the IESG saying about draft X?"_, _"is there a DISCUSS
  on this draft?"_, _"why hasn't draft X been published yet?"_ →
  `read_file_section(corpus, "ballots/<doc-name>.md", start_line=1)` for
  the full ballot, or `read_digest(corpus, "timeline",
  event_kind="ballot")` for chronology of position changes. A
  DISCUSS holds publication; report it as such.
- _"did anyone refute Alice's claim about X?"_, _"what were the
  responses to message [N]?"_ → `find_replies(corpus, file, chunk_idx)`.
  Returns every transitive reply to one specific message, full
  bodies, in date order. Use when you have a known message and
  want its follow-ups; use `read_topic` instead when you have a
  query and want anchored matches.
- _"level of support for X?"_, _"how many said +1?"_, _"who
  supported / objected?"_, _"did the chair call consensus, and is
  it visible in the traffic?"_ →
  `tally_positions(corpus, "<one thread or issue file>")`. Grounded
  per-author count (`+1`/`-1`/poll-option/`DISCUSS`) plus a **Chair
  statements** section surfacing procedural messages (`rough
  consensus`, `WGLC`, `adopting`, …). **Prefer this over relaying a
  chair's characterisation** — chair summaries are themselves
  sometimes disputed. Heuristic; coverage % is honest about misses.

If you're unsure which shape the question is, `overview` is the
safe default — it's cheap and points you at the rest.

## Catalogue queries: `read_digest(corpus, kind, ...filters)`

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
`doc-rfc`, `doc-wglc`, `ballot`}. Datatracker-sourced events (charter,
chair, doc-*, ballot) span the WG's full history, ignoring the
`--months` window. A standing `ballot` DISCUSS holds publication —
report it as such, not as "approved" because most ADs cleared.

## Substantive questions: `search_corpus(corpus, query, k=8)`

Returns top-k chunks with a snippet (ending `[truncated]` when the
chunk has more). Pivot to the source with `get_chunk_text` /
`get_chunks_batch` (pass `end_chunk_idx` for a ≤20-chunk range),
`read_file_section`, or `fetch_by_url` (resolve a pasted or cited
GitHub / mail-archive URL to cached content — corpus only, never the
live web).

Filters beyond the obvious `since`/`until`/`label`/`state`/
`file_pattern` (`"threads/%"`, `"drafts/%"`, …):

- **`group_by="file"`** — one row per file with a hit count, for
  **breadth** (*"which threads discuss MLKEM?"* → four threads, not
  fifteen overlapping chunks). Drop it for **depth**, where you want
  the actual quotes.
- **`author="<substring>"`** / **`role="Chair"`** (or `Author` /
  `Editor` / `AD`) — scope to a person or a structural role, matched
  against the chunk's section header (*"what did Rescorla say"*, *"what
  did the chairs decide"*). Combine to scope to one chair.
- **`snippet_chars=N`** — raise the per-hit snippet for long-form
  synthesis (dial `k` down to compensate).

## File types you'll encounter

All paths are relative to the WG's cache root (`<corpus>/files/`).

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
- **`meetings/<code>/agenda.md`** — the session agenda (what the
  chairs *planned* to cover; minutes are what happened).
- **`meetings/<code>/slides/<name>.pdf.txt`** — extracted slide
  text. The `.pdf.txt` carries a meeting-context header so chunks
  deep inside still have attribution.
- **`meetings/<code>/transcripts/<YYYYMMDDHHmm>.md`** — session
  transcripts (with meeting-context header).
- **`meetings/<code>/polls/<YYYYMMDDHHmm>.md`** — session poll
  records from Datatracker. Read in full when answering "where was
  the room leaning on X?" — polls aren't consensus but they're
  signal.
- **`ballots/<draft-name>.md`** — IESG ballot for a draft, with the
  latest position per Area Director (DISCUSS / Yes / No Objection /
  Abstain / Recuse) and full DISCUSS text inline. Present only for
  drafts with ballot activity in the `--months` window. Read in full
  when answering "what's the IESG saying about draft X" — these are
  load-bearing for "is this draft moving / blocked / done."
- **`group.md`** — WG-level metadata: status (active / concluded),
  parent area, and "Additional Resources" (repositories, home page,
  chat, alternate list archives). The `overview` surfaces these; read
  the file for the raw links.
- **`digests/<kind>.md`** — the catalogue digests (`index`,
  `issues`, `threads`, `people`, `timeline`). Read via `read_digest`,
  not `get_chunk_text`.

`list_files(corpus)` shows per-file chunk counts so you can bound
`get_chunk_text` ranges without probing.

## Reading the WG's current position

For *"where does the WG stand right now on X"*: start with the
chair's most recent statement, then reconstruct the arc. Try
`read_digest(corpus, "issues", state="closed", label="X")` for chair-
resolved decisions, or `search_corpus(corpus, "X consensus|resolution|
wglc", sort="date")` and scan the latest hits whose chunk title
carries a `(Chair)` role tag — those are usually the load-bearing
posts.

`search_corpus(sort="date")` is the lower-level cousin of
`read_topic`: chronological, but the unit is a chunk (snippet, not
full text) with no reply-expansion — relevance hits in date order.

## Message numbering and `chunk_idx`

In per-thread and per-issue files, the `### [N]` heading number IS
the chunk_idx — message `[3]` in the file is `chunk_idx=3` in the
embedding index. `get_chunk_text`, `find_replies`, `read_topic`'s
output all use the same number. When the user asks about
"message 3", either reference is correct.

## Canonical names

Identities are pre-consolidated. "Mark Nottingham" in any digest,
hit, or file is the same actor who appears as `mnot` on GitHub, in
DMARC-rewritten addresses, and via the Datatracker relay. Don't
fabricate identity links — if a GitHub login isn't already mapped
in the `people` digest, say so.

## How the IETF works

A few interpretive norms that shape how to read the corpus:

- **Individuals, not employers — but implementer signal is real.**
  People participate as individuals, not as company representatives.
  Don't attribute a position to a company ("Cloudflare opposes X")
  based on the author's affiliation. Only treat something as a
  *company* position when the author explicitly frames it that way
  ("my company…", "speaking for X…", "as an employee of Y…").

  *That said*: who ships running code matters. "Rough consensus and
  running code" weighs implementer voices, and clustering of stated
  affiliations across an argument is itself news. The `people`
  digest records affiliations from drafts and GitHub with source
  provenance (`Cloudflare (draft, github)` = corroborated; `(github)`
  alone = self-reported only); blank = no documented signal, NOT
  "Independent." Two rules of thumb:
  1. **Aggregate, don't attribute.** "8 of 12 stated supporters are
     from organisations shipping TLS stacks" — fine. "Cloudflare
     supports X" — not fine, unless they said so.
  2. **Email domain ≠ affiliation.** `mnot.net` is Mark Nottingham's
     personal domain; he ships drafts as Cloudflare or Independent
     depending on the draft. Use the `affiliations` field on Person
     / the people digest, never the From-header domain.

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

## Anti-patterns

- **Don't reflexively crawl IETF sites.** Default to the corpus; if
  something's missing, the user re-gathers (`ietf-llm <corpus>`). Live
  fetches are fine when genuinely needed — see the intro for the rule.
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
