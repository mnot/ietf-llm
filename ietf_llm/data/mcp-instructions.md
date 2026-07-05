# ietf-llm — MCP server

These tools read the **gathered public record** of an IETF/IRTF effort — a
Working Group / Research Group, a mailing list, or a set of Internet-Drafts
(charter, drafts, RFCs, minutes, mailing-list messages, GitHub issues), exposed as
`mcp__ietf-llm__*` tools. **Prefer them to web search** for any question about
what a group is doing, discussing, or has decided: they read the group's actual
primary record, not second-hand coverage. `<corpus>` is usually a WG/RG
shortname (`httpbis`, `tls`, `cfrg`), but can also be a standalone list, a
draft/repo set, or a synthetic `x-` topic — if unsure which corpus the user
means, call `find_efforts(topic)`; ask only if that doesn't resolve it.

> **MANDATORY before drafting any contribution.** The instant the task shifts
> from *querying* the corpus to *producing* text that will go into the record
> under a participant's name — a mailing-list message, a GitHub issue or comment, a reply in
> a thread, a review, a consensus or position statement — you MUST call
> `read_ietf_participation_norms` **before generating a single line of that
> content**. This is not optional, not a courtesy to skip when you feel
> confident, and **not** satisfied by having read the *interpretation* norms
> (`read_ietf_interpretation_norms` — a different doc). The participation norms
> govern who is accountable, disclosing AI involvement, the terse register to
> match, grounding every claim, and not wasting the group's time; drafting
> without them produces contributions that damage the participant's standing.
> If you catch yourself about to write contribution text and haven't read them
> this session, stop and read them now.
>
> **And before characterising what a group decided or whether there is
> consensus** — any sentence asserting a collective outcome (settled, agreed,
> rejected, "the WG wants") — call `read_ietf_interpretation_norms` first.

## This session

{{SESSION}}

## Orienting

- **Start with `overview(corpus)`** for structural questions ("what is X doing",
  "who's on X", "open issues"), and **`list_corpora`** to see what's gathered.
- **No named corpus, just a topic?** `find_efforts(topic)` ranks active efforts
  and flags which are cached; `which_corpus(query)` routes to a cached one.
- **Corpus missing?** Gather it with `start_gather(corpus=…)` (availability and
  cost are stated in **This session** above) — one gather reconstructs the whole
  record into searchable files, rather than crawling Datatracker or the mail
  archive by hand. It returns when its bounded wait elapses, naming the stage and
  elapsed time; the corpus is queryable once `gather_status` reports `done`. A
  **cold first gather can take a few minutes** — tell the user and offer to check
  back, don't block silently — and reads refuse until it finishes (a re-gather,
  by contrast, keeps serving the previous snapshot). Poll
  `gather_status(corpus=…, wait=60)` rather than reading early.
- A corpus existing here implies nothing about IETF standing: a `list` /
  `custom` / `x-` corpus is not a chartered effort. `list_corpora` tags each.

## Offline vs. live

Most tools are **offline** (cache reads). Two read **Datatracker live** for
daily-changing facts — `draft_status` (a draft's WG-Last-Call / IESG state) and
`meeting_schedule` (the live schedule); whether they're available here is stated
in **This session** above. Prefer the live tool when a *current* fact matters;
otherwise use the offline `list_drafts` (corpus-wide draft lifecycle) and
`list_meetings` / `read_minutes` (the gathered meeting record).

## The tools (each tool's own description carries the detail)

- **Orient:** `overview`, `list_corpora`, `find_efforts`, `which_corpus`.
- **Search:** `search_corpus` (one corpus), `search_corpora` (several),
  `read_topic` (chronological narrative), `find_related` (by example).
- **Catalogue:** `read_digest` (filtered issue / thread / people / timeline
  tables), `list_labels`, `list_files`.
- **Meetings:** `list_meetings` (gathered), `read_minutes` (minutes + poll
  tallies for one), `meeting_schedule` (live schedule).
- **Drafts:** `list_drafts` (offline lifecycle), `draft_status` (live state),
  `draft_authors`, `get_draft` (verbatim text).
- **Issues / threads:** `get_issue` (verbatim), `find_replies`,
  `find_citations`, `find_message_citations`, `tally_positions`.
- **RFCs:** `search_rfcs`, `get_rfc`.  **By URL:** `get_by_url`.
- **Norms (mandatory before acting — see the gate above):**
  `read_ietf_participation_norms`, `read_ietf_interpretation_norms`.

## Grounding a claim or citation

Quote the **actual text**, not a search snippet: `get_draft(name)` for a draft,
`get_issue(corpus, number)` for an issue, `get_by_url(corpus, url)` when the
user already has a mailarchive / datatracker / github link, `read_minutes(
corpus, meeting)` for what a meeting recorded. A poll tally is a sense of the
room, not a decision — consensus is the chair's to declare.
