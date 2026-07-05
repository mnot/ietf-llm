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

## Orienting

- **Start with `overview(corpus)`** for structural questions ("what is X doing",
  "who's on X", "open issues"), and **`list_corpora`** to see what's gathered.
- **No named corpus, just a topic?** `find_efforts(topic)` ranks active efforts
  and flags which are cached; `which_corpus(query)` routes to a cached one.
- **Corpus missing?** Gather it with `start_gather(corpus=…)` — one gather
  reconstructs the whole record into searchable files, rather than crawling
  Datatracker or the mail archive by hand. Whether gather is available is a
  property of **this session**, not something to infer from the transport (don't
  assert you're on a "read-only / HTTP replica" backend): it is available iff the
  `start_gather` tool is present — which may be **deferred**, so do a tool search
  for it before concluding it is absent — and `list_corpora` states, at the
  bottom, this session's **deployment mode** (local single-user vs possibly
  shared) and gather availability. Read both from that line, never from the
  transport-flavoured wording in tool descriptions: on a **local stdio** server a
  gather costs only this user, so the "a wide gather costs everyone" cautions —
  which are scoped to *shared* deployments — do not apply; don't hedge or ask
  permission you don't need. Only if gather is genuinely unavailable, tell the
  user to run `ietf-llm <name>` locally. `start_gather` returns when its bounded
  wait
  elapses, naming the stage it reached (`stage 18/19 (embedding index)`) and how
  long it has run; the corpus is queryable once `gather_status` reports `done`.
  A cold first gather could take a few minutes — tell the user that and offer to
  check back once it reports `done`, rather than blocking silently. While a
  **first** gather runs there is nothing to serve yet, so reads refuse with the
  stage and how many are left — poll `gather_status(corpus=…, wait=60)` rather
  than reading early. A **re-gather** keeps serving the previous snapshot
  (flagged as a refresh in progress), so you can keep querying it.
- A corpus existing here implies nothing about IETF standing: a `list` /
  `custom` / `x-` corpus is not a chartered effort. `list_corpora` tags each.

## Offline vs. live

Most tools are **offline** (cache reads) and always work. Two reach
**Datatracker live** for facts that change daily — `draft_status` (one draft's
WG-Last-Call / IESG state) and `meeting_schedule` (the live schedule); on a
read-only deployment they are disabled and simply **absent** from the tool
surface. As with gather, that is a property of *this session*: read it from the
tools you actually have (searching for deferred ones), never from the transport.
Prefer the live tool when a *current* fact matters; where it is unavailable, fall
back to its offline counterpart — `list_drafts` (corpus-wide draft lifecycle) and
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
