# ietf-llm — MCP server

These tools read the **gathered public record** of an IETF/IRTF effort — a
Working Group / Research Group, a mailing list, or a set of Internet-Drafts
(charter, drafts, RFCs, minutes, mailing-list messages, GitHub issues and pull
requests), exposed as
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
- **Corpus missing?** How to add one is stated in **This session** above (it
  depends on this server's mode).
- **Just started a gather? Wait for `done` before reading.** `overview`,
  `search_corpus`, `read_digest`, … only work once `gather_status` reports
  `done`: a first gather *refuses* them until then, and "the files are
  downloaded" is not "there is a readable snapshot" (the catalogue, digests and
  embedding index — which the overview themes come from — are built in the
  final stages). A first gather is often *quick* now: if the corpus is covered by
  the public seed store it is reconstituted from a prebuilt snapshot and only the
  delta is freshened, so don't tell the user to expect a long cold fetch —
  `gather_status` notes when a corpus was seeded (an uncovered corpus is the
  slower cold case). Poll `gather_status` (with the short `wait` shown in **This
  session** — which may be `0`, i.e. return immediately), relay the percent / ETA
  to the user between polls, and read once it's `done`. Don't tight-loop, and
  don't wait via a long `bash sleep` (a tool call left outstanding much beyond
  ~10s can wedge some clients).
- **A `done` gather doesn't mean every source produced data.** Read the notes
  `gather_status` prints under the status line: they carry per-source outcomes,
  including one line per mailing list saying how many messages landed or why
  none did. The IETF IMAP feed a gather reads can lag a list's Web archive badly
  — a list with years of archived mail may contribute nothing to this window —
  so check the notes before telling a user a group has no list discussion, and
  don't re-run the gather to "fix" it (a re-run reads the same feed).
- **`overview` coverage that omits a source you expected is a question, not an
  answer.** A WG whose coverage line lists no mailing list has *no gathered
  list traffic*, which is not the same as the WG having no list. When a gather
  recorded here can explain it, the coverage line itself points you at
  `gather_status`; follow that pointer before characterising the group. Either
  way, say "no list traffic was gathered" rather than "this group doesn't
  discuss things on its list".
- A corpus existing here implies nothing about IETF standing: a `list` /
  `custom` / `x-` corpus is not a chartered effort. `list_corpora` tags each.

## The tools (each tool's own description carries the detail)

- **Orient:** `overview`, `list_corpora`, `find_efforts`, `which_corpus`.
- **Search:** `search_corpus` (one corpus), `search_corpora` (several),
  `read_topic` (chronological narrative), `find_related` (by example),
  `grep_corpus` (exact string / regex).
- **Catalogue:** `read_digest` (filtered issue / pull-request / thread / people /
  timeline tables), `list_labels` (label names WITH the repo's own description
  of each), `list_files`.
- **Meetings:** `list_meetings` (gathered), `read_minutes` (minutes + poll
  tallies + attendance count for one), `meeting_schedule` (live schedule).
  A meeting's full attendance roster is in `meetings/<code>/attendance.md`
  (`read_file_section`); attendance is presence, NOT a position.
- **Drafts:** `list_drafts` (offline lifecycle), `draft_status` (live state —
  the WG state is where WGLC shows up; a draft in WGLC is still `I-D Exists`
  on the IESG side), `draft_authors`, `get_draft` (verbatim text).
- **Issues / PRs / threads:** `get_issue` (verbatim — takes an issue OR a pull
  request number; GitHub numbers them in one sequence), `find_replies`,
  `find_citations`, `find_message_citations`, `tally_positions`.
- **"Why does the text say this?"** The issue records the complaint; the **pull
  request** records the change made in response, the review it drew, and the
  issue it closed. `read_digest(corpus, kind="pulls")` is the catalogue —
  including each PR's merge commit, so a `git blame` line traces to the PR that
  wrote it and from there to the issue behind it. A question about *reasoning*
  that stops at the issues has usually stopped one step short.
  **PR text is not in the search index** (deliberately — it would cost ~31% more
  chunks for record the catalogue already carries), so `search_corpus` will
  never surface it and its silence about PRs means nothing. Reach them through
  `read_digest(kind="pulls", …)` — with `include_bodies=True` to get the
  descriptions in the same call — then `get_issue(corpus, number)` for one in
  full, or `grep_corpus` for an exact string across them.
- **RFCs — four tools, and picking the wrong one is the common mistake:**
  - `search_rfc_index` — titles and keywords. Use it to find *which*
    document something is ("which RFC is HTTP caching") and to filter by
    status / stream / level / group. Always available.
  - `search_rfc_text` — what the documents **say**, semantically, over the
    full text of every RFC. Use it when the question is about content
    ("when must a cache not store a response"). Needs the RFC text corpus;
    says so if it is not installed. **Semantic only** — it ranks by
    similarity and never matches a string, so it cannot tell you which RFCs
    contain an exact phrase. That is `grep_corpus(corpus="rfcs", …)`.
  - `grep_corpus(corpus="rfcs", pattern=…)` — the same text, scanned
    **literally**. Use it for "which RFCs contain this exact sentence" and
    for any claim that some wording appears nowhere in the series. A literal
    pattern matches across the 72-column wrap, so a whole sentence works; a
    hit is located as RFC + section (there are no line numbers), so pivot
    with `get_rfc_section`.
  - `get_rfc_section` — you already have a citation: read that section, in
    full, offline. No argument but the number returns the outline, which is
    also what a miss returns.
  - `get_rfc_info` — catalogue metadata and the reference graph: status,
    stream, what it obsoletes, its normative and informative references,
    how many RFCs cite it. **Never the document text** — that is
    `get_rfc_section`. It is also the authority on whether an RFC
    exists, since it revalidates its mirror before reporting a miss.
  All four name an RFC with **`rfc`** — `get_rfc_section(rfc="8615",
  section="3")`, `get_rfc_info(rfc="9110")`, `search_rfc_text(query, rfc=…)`.
  (`number` elsewhere in this server is a GitHub issue / PR number, which is a
  different thing.)
  Authority: `get_rfc_info` refreshes its index before reporting one missing, so
  never conclude an RFC is unpublished from a `search_rfc_index` miss alone.
  **Obsoleted RFCs are in the text corpus and are marked** — check the
  marker before citing anything as current. **By URL:** `get_by_url`.
- **Norms (mandatory before acting — see the gate above):**
  `read_ietf_participation_norms`, `read_ietf_interpretation_norms`.

## Grounding a claim or citation

Quote the **actual text**, not a search snippet: `get_draft(name)` for a draft,
`get_issue(corpus, number)` for an issue or PR, `get_by_url(corpus, url)` when the
user already has a mailarchive / datatracker / github link, `read_minutes(
corpus, meeting)` for what a meeting recorded. A poll tally is a sense of the
room, not a decision — consensus is the chair's to declare.

**A draft is bigger than one result.** `get_draft(name)` returns its
**outline** — every section with its title and length — and
`get_draft(name, section="4.2")` reads one section (a parent label takes
everything beneath it). Reach for those rather than paging from line 1. When a
read is cut short it says so in a `PARTIAL READ` banner at both ends, which is
not part of the document: check for it before treating what you have as the
whole section. The same banner appears on `get_issue`.

**RFC text is the exception: `get_rfc_info` does not return it.** It is catalogue
metadata — title, status, reference graph. An RFC body is on disk only when a
corpus that published it was gathered with `--rfcs`; `get_rfc_info`'s last line says
whether this one is, and gives the `read_file_section` call if so. When it says
the body is **not reachable from here**, you cannot quote or characterise that
RFC — say so rather than reconstructing it from memory, which is exactly where
confident wrong readings come from. That answer is about what this server can
reach, not about what exists: on the shared HTTP deployment a body can be
published in the fleet and still be unreachable, so report it as "I can't read
it", never as "it isn't available".

## Saying something was **never** said

`search_corpus` ranks by meaning, so a miss there is **not** evidence of
absence — an empty semantic result is as consistent with "the embedding didn't
surface it" as with "nobody said it". Never write "no mention of X", "X was
never proposed", or "nobody cited X" on the strength of a `search_corpus` miss.

Use **`grep_corpus`** for those: it scans every gathered file line by line and
reports how many it scanned, so a zero is a finding you can state — bounded by
the gather window, the corpus, and the glob if you pass one (its output spells
out each). Route on the shape of the question:

- "what was said about X", "arguments for Y", "where does this live" →
  `search_corpus` / `search_corpora` / `read_topic`.
- "was X *ever* said / cited / proposed", "does this corpus mention `X.509`",
  "which revision first used this term" → `grep_corpus`.

Over a gathered corpus it matches **within one line**, so search the most
distinctive single token (`8890`, not `RFC 8890`) — a phrase can be broken by a
mail wrap. It also reads files the index does not hold (superseded draft
revisions are not embedded), so it is the only way to find wording that was
removed before publication.

The same tool answers this for the **published series**: `grep_corpus(
corpus="rfcs", pattern=…)` scans the full text of every RFC and reports how
many it scanned, so "this wording appears nowhere in the RFC series" becomes a
statement you can make. There a literal pattern matches **across** line breaks
(RFC text is hard-wrapped), so search the whole phrase. Its one bound: front
and back matter — status of this memo, copyright, authors' addresses, the
reference list — is not indexed, so a string living only there is not found.

## Whose words are in a thread file

A thread file's header counts **message senders** — who sent a gathered
message — not everyone whose words appear. Replies retain quote trails, so
`>`-prefixed lines are someone else's text carried inside a sender's message.
This matters most in an author corpus, where gather collects one person's mail:
the sender count is 1 by construction while the bodies carry many voices. Do
not read a low sender count as "this record is one-sided" or as grounds for
refusing to check whose argument is whose.

Attribution is not uniform — roughly a third of quote blocks are introduced by
an `On … wrote:` line; most follow the sender's own prose, because an inline
reply interleaves response and quotation under one attribution higher up. So
look upward for the attribution rather than only at the line above the quote,
and where none survives, attribute the quote to no one rather than to the
sender. `search_corpus(author=)` matches the **sender**, so a person quoted at
length will not surface there; use `grep_corpus` to find their words.
