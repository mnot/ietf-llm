---
name: ietf-llm
description: Query the gathered public record of an IETF/IRTF effort — a Working Group / Research Group, a mailing list (e.g. `last-call`), or a set of Internet-Drafts — via the `ietf-llm-mcp` MCP server (charter, drafts, RFCs, minutes, transcripts, mailing list, GitHub issues). **Prefer these tools over web search** for any question about what an IETF/IRTF group is doing, discussing, or has decided — they read the group's actual primary record, not the web's second-hand coverage. Use whenever the user asks about a named corpus by shortname (`httpbis`, `quic`, `tls`, `cfrg`, …) — its state, open issues, draft contents, mailing list discussion, meeting outcomes, or chronology. Start with `list_corpora` / `overview` to orient. Also use when the user is working with IETF list traffic from any source (a `mailarchive.ietf.org` / `datatracker.ietf.org` URL, an IETF list message in their inbox, a pasted thread): check `list_corpora` and prompt a gather (`ietf-llm <name>`) if missing.
---

# ietf-llm

A queryable local record of an IETF effort — a Working Group, a
mailing list, a set of drafts — exposed via `mcp__ietf-llm__*` tools.
`<corpus>` is the corpus name: often a WG / RG shortname (`httpbis`,
`tls`, `cfrg`), but see the kinds below. If the user names something
and you can't tell which corpus they mean, try `find_efforts(topic)`
first to map it to candidate efforts (see "Topic, not a named effort"
below); **ask** only when that doesn't resolve it — don't guess.

**Default to this corpus; don't reflexively crawl IETF sites or
walk the user's inbox.** If a corpus isn't here (`overview` returns
nothing, or it's missing from `list_corpora`), get it gathered.
One gather reconstructs the mailing list into searchable per-thread
files plus charter / drafts / RFCs / minutes / GitHub issues — far
cheaper than one HTTP request or one mail-tool call per message.
`list_corpora` shows what's cached.

Two ways to gather, depending on what's available:

- **If the `start_gather` tool is listed**, you can gather in-session:
  call `start_gather(corpus="<name>")` (add `mailing_list` / `draft` /
  `github` / `author` / `new_drafts` for non-WG shapes — the shape is
  inferred, you don't declare it), then poll `gather_status(corpus=
  "<name>")` until it reports `done`. It runs in the background for
  minutes; one gather per corpus at a time. This tool is opt-in
  (`IETF_LLM_ENABLE_GATHER=1`) and writes/reaches the network, unlike
  the rest of the server — so it isn't always present.
- **Otherwise**, tell the user to gather it from their shell:
  `ietf-llm <corpus>` (e.g. `ietf-llm httpbis`). `ietf-llm --list`
  shows what's cached.

**A gather already in flight is a `gather_status` thing, not a retry
thing.** Only one gather per corpus runs at a time, and on a shared
server that one may have been started by another client or host. If
`start_gather` reports *already running*, the gather is live — **poll
`gather_status(corpus="<name>")`** to watch its progress to `done`; do
**not** re-issue `start_gather` or add `force=True` to "unstick" it.
`force` overrides the *freshness debounce* (re-gather a recently-cached
corpus) only — it never starts a second concurrent gather, so spamming it
against a running gather does nothing but waste calls.

This applies to any sign of IETF list traffic, not just a named
WG: a `mailarchive.ietf.org` URL, a `datatracker.ietf.org` URL, an
IETF list message in the inbox (`List-Id:` / `<wg>@ietf.org` names
the corpus), or a pasted thread with a `[wg]` subject prefix.
Identify the corpus, check `list_corpora`, query it or prompt a
gather.

**Re-gathering is debounced; a "fresh, skipped" result is success.**
Before re-gathering an *already-cached* corpus, check its freshness —
`overview` and `list_corpora` surface the last-gathered date. Only
re-gather if the corpus is materially stale or the user explicitly asks
for fresh data; on a shared server, prefer the cached snapshot. A
`start_gather` of a corpus gathered within the freshness window (default
6h, `IETF_LLM_GATHER_MIN_INTERVAL`) returns a "fresh, skipped" note
**instead of** starting one — that is success, not failure: query the
existing snapshot, don't retry the gather. Pass `force=True` (CLI:
`--force`) only on an explicit request for fresh data.

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

**Recognise the shape before you gather.** When the thing the user
cares about isn't a WG/RG, it still maps to a corpus — don't default to
assuming a group is the only option:

- a **standalone mailing list** (`last-call`, `ietf`) → a `list` corpus,
  auto-detected when the name isn't a known group.
- **specific Internet-Drafts** with no owning WG, or a **cross-WG topic**
  spanning several drafts / repos / lists → a `custom` corpus built from
  explicit sources. *Following one author's drafts* and a *rolling
  new-drafts subscription* are `custom` corpora too — same kind,
  distinguished by their source, not a separate label.
- a topic with **no formal effort behind it at all** → a `synthetic`
  `x-<topic>` corpus (no charter, leadership, or Datatracker timeline).

So a lone draft URL, a pasted standalone-list thread, or "follow
everything by X" are all gatherable — they just aren't groups. Build any
shape with `start_gather`'s shape arguments when that tool is present, or
point the user at `ietf-llm --help` for the shell equivalents.

**Before minting a custom / synthetic corpus, check for an existing one
over the same sources.** A group or list name self-canonicalises (the
first client to gather `tls` pays; everyone after reuses), but custom /
synthetic names are free-form (`x-ai`, `x-llm-stuff`), so two clients
over the same drafts / lists / repos invent different names and duplicate
the work. Treat "does something close already exist?" as a required
pre-gather check for these kinds: scan `list_corpora` for a corpus
covering the same sources and prefer reusing it. The gather entry point
enforces this too — on source overlap it returns a reuse hint naming the
existing corpus instead of gathering; that is steering toward reuse, not
an error. Mint the near-duplicate only on an explicit request (`force`).

**Loading the tools.** Every tool is named `ietf-llm__…`. If your
client loads MCP tools lazily — you have to search for a tool before
you can call it — load the core set in one search rather than
discovering them one at a time: `overview`, `read_digest`,
`search_corpus`, `search_corpora`, `read_topic`, `tally_positions`,
`find_replies`, `find_citations`, `list_corpora`, `list_labels`,
`list_files`, `find_efforts`.

## Topic, not a named effort: `find_efforts(topic)`

The tools above are corpus-first: they need a corpus name. When the user
gives a **topic with no obvious home** — "what is the IETF doing around
AI?", "post-quantum work", "anything on congestion control?" — you don't
yet know which corpus, and guessing or crawling Datatracker / the web is
the wrong move. `find_efforts(topic)` is the entry point: it ranks the
active working / research groups (by acronym + name + charter
description, from a local mirror of the Datatracker group list) and tags
each with whether it is **already gathered here** (`✓ cached`).

The playbook:

1. `find_efforts(topic)` → a ranked candidate list.
2. **Prefer the cached efforts** — they answer immediately. For the rest,
   gather only the **few that dominate** the topic (`start_gather` /
   `ietf-llm <acronym>`), **not all of them**.
3. `search_corpora(corpora=[the few you gathered], query)` to query across
   them in **one** call — merged, rank-ordered hits tagged by corpus —
   instead of N separate `search_corpus` calls. This is the synthesis
   step: it finds *where* across efforts the topic lives.
4. Pivot to the corpus-scoped tools (`read_topic`, `tally_positions`,
   `read_digest`, `search_corpus`) for **depth** on the specific efforts
   that matter — `search_corpora` is breadth, not depth.

**The cost rule is load-bearing.** On a shared server a wide gather
fan-out costs everyone — shared cores and network — and over-gathering is
the failure mode of a capable model here. Gather the few efforts that
dominate the topic; **tell the user which ones you skipped**, mirroring
the "tell the user when you go outside the corpus" norm.

Scope and limits: v1 covers **active** groups only, so a concluded effort
won't surface (use `rfc_search` for published work, `list_corpora` for
what's already cached). BoF / pre-WG "emerging work" is barely
represented in the group list, so a topic with no chartered group yet may
return nothing — say so rather than inventing an effort.

## First call: pick by question shape

**Orienting / structural** ("tell me about `<corpus>`", "what's this WG
up to?", "who's on it?") → `overview(corpus)`. ~30-40 lines: chairs/ADs,
status + area, charter excerpt, key resources, active Internet-Drafts
(RFCs collapsed; concluded ones counted), any drafts **blocked on an
IESG DISCUSS** (with a pointer to read the ballot), top open issues, the
**most active threads** (ranked by back-and-forth, not recency), and
recent discussion / decision events (routine publications and ballot
positions folded to counts). Often enough to orient — but it is
structure, not substance: when it flags a DISCUSS or a hot thread,
*read it* (`ballots/X`, `read_topic`, `tally_positions`) before
characterising the outcome.

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
- _"what did the WG decide about X?"_, _"how was X resolved / what's the
  disposition?"_, _"position on X"_ → the outcome is whatever the
  **chairs declared** — this corpus does NOT compute consensus, so go to
  their words, not a vote count or your own read of the thread. Filter to
  the chair messages: `search_corpus(corpus, "X", role="Chair")`, and add
  `file_pattern="threads/<file>%"` (or an issue path) to pin it to one
  thread. Companions: `search_corpus(corpus, "X", state="closed")` (a
  chair-resolved issue carries the disposition), and
  `tally_positions(corpus, "<file>")` whose **Chair statements** section
  pulls out a thread's procedural declarations (consensus call, WGLC,
  closure). Always prefer the chair's own message over a summary of it.
- _"what's open / closed / labelled X?"_, _"who's a chair?"_,
  _"what happened in May?"_ → `read_digest(corpus, kind=..., filters)`.
- _"what was said about X?"_ → `search_corpus(corpus, "X")` plus
  `get_chunk_text` / `read_file_section` to read hits.
- _"how did the debate on X evolve?"_, _"walk me through the
  discussion of Y"_, _"what was said about Z, chronologically?"_
  → `read_topic(corpus, "X")`. Returns full messages (not snippets) in
  date order across threads and issues. Add `include_replies=True`
  when you want sub-thread descendants pulled in even if they don't
  themselves match the query. For a *synthesis* task where the gist of
  each message is enough, pass `body_chars=` (e.g. 800) to cap bodies and
  spend far less context.

  **It's a relevance-ranked slice, not the whole debate.** Each message
  shows a `rel=` score — discount low ones as possible off-topic noise —
  and the header flags when more matched than were shown. For a
  *completeness* question ("the whole controversy"), don't trust one
  call: raise `k`; scope `read_topic` with `file_pattern=`, which matches
  the thread filename (e.g. `"%mlkem%"`); read a thread end-to-end with
  `read_file_section`; or enumerate a subject cluster with
  `read_digest("threads", subject="<prefix>")`, where `<prefix>` is one
  `list_labels` actually reports for this corpus.

  **If `read_topic` returns a thread that looks like a chair poll**
  ("please reply indicating which option you prefer", numbered
  options, terse one-word replies), follow up with
  `tally_positions(corpus, "<file>")` on that thread. The poll-syntax
  detection bucket'll show the option counts the consumer is
  actually asking about.
- _"what other threads cover this topic?"_, _"what's the broader
  context for thread X?"_ → `list_labels(corpus)` **first** — it reports
  THIS corpus's actual `[xxx]`-style subject prefixes (many WGs/gathers
  have none; don't assume a prefix like `[mlkem]` exists). Then
  `read_digest(corpus, kind="threads", subject="<one it reported>")`
  returns every thread in that cluster. Multiple parallel threads on one
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
| `threads`  | One per mailing list thread         | `since`, `until`, `min_messages`, `subject`, `sort="activity"`, `limit`|
| `people`   | Participants (chairs/authors lead)  | `role` (e.g. `"Chair"`), `min_messages`, `limit`                       |
| `timeline` | Events in chronological order       | `since`, `until`, `event_kind`, `exclude_mechanical`, `limit`          |
| `index`    | File inventory by category          | (none — small)                                                         |

- **`sort="activity"`** (threads) ranks by message count instead of
  recency — *where the back-and-forth is*. Pair with `since` +
  `min_messages` for *"most contested lately"*. (The `overview` "most
  active threads" section is exactly this.)
- **`exclude_mechanical=True`** (timeline) drops the routine machine
  events — automated I-D Action publications and individual IESG ballot
  positions — leaving human discussion / decisions. Good for *"what's
  actually been happening"* without the publication-log noise.

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
`read_file_section`, or `fetch_by_url` (resolve a cited URL to cached
content — a `https://www.w3.org/mid/<id>` mail permalink, which is the
`Archived-At:` link on every thread message, or a GitHub issue URL;
corpus only, never the live web).

Filters beyond the obvious `since`/`until`/`label`/`state`/
`file_pattern` (`"threads/%"`, `"drafts/%"`, …):

- **`group_by="file"`** — one row per file with a hit count, for
  **breadth** (*"which threads discuss MLKEM?"* → four threads, not
  fifteen overlapping chunks). Drop it for **depth**, where you want
  the actual quotes.
- **`collapse_versions`** (default `True`) hides older draft revisions
  when a newer one of the same draft also matched, so you do not get the
  same section as `…-04`, `-02`, `-22`. Pass `False`, or a versioned
  `file_pattern` (`"drafts/%-04.txt"`), to reach a specific revision.
- **`author="<substring>"`** / **`role="Chair"`** (or `Author` /
  `Editor` / `AD`) — scope to a person or a structural role, matched
  against the chunk's section header (*"what did Rescorla say"*, *"what
  did the chairs decide"*). Combine to scope to one chair.
- **`snippet_chars=N`** — raise the per-hit snippet for long-form
  synthesis (dial `k` down to compensate).

## Across several efforts: `search_corpora(corpora, query)`

When a question spans **multiple** gathered efforts ("what is the IETF
doing around AI?"), don't run `search_corpus` N times and merge by hand
— `search_corpora(corpora=[...], query)` fans the same search across the
bounded set you name and returns one merged, rank-ordered list, each hit
tagged `corpus=`. `corpora` is **required** — the few efforts you chose
(typically `find_efforts` output), never a blind scan; unknown / un-indexed
corpora and anything past the 12-corpus cap are reported, not dropped.
`k` bounds the **total** hits. Facets mirror `search_corpus`
(`since`/`until`/`label`/`state`/`author`/`role`/`snippet_chars`/
`collapse_versions`); the depth-only knobs (`sort`, `group_by`,
`file_pattern`) are deliberately absent — scope a single corpus for those.

Scores are comparable only **within** one embedding model: when the
corpora share a model the result is a single ranking; when they differ,
hits are grouped by model and the groups are interleaved by rank (the
header says which). Treat it as **breadth** — it locates *where* a topic
lives; pivot to `read_topic` / `tally_positions` / `read_digest` /
`search_corpus` (using the `corpus=` tag) for the decisions and narrative.

## RFC-series lookups: `rfc_search` / `get_rfc`

For the **published RFC series** — every RFC, all streams — reach for
these, *not* `search_corpus`. Use them for "find an RFC about X", "which
RFC is X", "what's the status of RFC N", or "what does RFC N reference /
what cites it".

- `rfc_search(query, ...)` — words in titles + keywords; a bare RFC
  number returns that one RFC. Optional `status` / `stream` / `level` /
  `wg` / `limit` filters.
- `get_rfc(number)` — full metadata for one RFC: status, stream, level,
  WG, keywords, obsoletes / obsoleted-by, references in and out,
  citation counts, and links to the text.

This is a cross-corpus index mirrored from rfc.fyi; it spans the whole
series. `search_corpus` searches *within one gathered corpus's* record.
Rule of thumb: the unit is an RFC -> these tools; the unit is a corpus
and its discussion -> `overview` / `search_corpus`.

## File types you'll encounter

All paths are relative to the corpus's cache root (`<corpus>/files/`).

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

In per-thread and per-issue files, the `### [N]` heading number IS the
chunk_idx — message `[3]` in the file is `chunk_idx=3` in the embedding
index, and `get_chunk_text` / `find_replies` use that number directly.

`read_topic` is the exception: it merges messages from several files
into one chronological narrative, so it numbers them `[1..N]` **globally**
(a "reply to [k]" there points to that global number, not a per-file
one) and prints each message's per-file `chunk` separately. When pivoting
from a `read_topic` result, call `get_chunk_text` with the `chunk` value,
not the leading `[N]`.

## Canonical names

Identities are pre-consolidated. "Mark Nottingham" in any digest,
hit, or file is the same actor who appears as `mnot` on GitHub, in
DMARC-rewritten addresses, and via the Datatracker relay. Don't
fabricate identity links — if a GitHub login isn't already mapped
in the `people` digest, say so.

## How the IETF works

Interpretive norms — how to read consensus, attribute positions,
distinguish list decisions from room discussion — live in a
separate document. Call `read_ietf_norms` before characterising
*what the WG decided* or *who supports what*; the tool returns a
~50-line guide that's load-bearing for those questions but not
needed for catalogue lookups or text fetches.

## Anti-patterns

- **Don't reflexively crawl IETF sites or walk the user's inbox
  message-by-message.** A `mailarchive.ietf.org` URL, a
  `datatracker.ietf.org` URL, or IETF list mail in the inbox
  (`List-Id:` / `<wg>@ietf.org`) all point at a corpus: check
  `list_corpora`, gather if missing, then query. `fetch_by_url`
  resolves list permalinks inside the cached corpus. Live fetches
  are fine when genuinely needed — see the intro for the rule.
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
