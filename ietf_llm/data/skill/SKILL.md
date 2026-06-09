---
name: ietf-llm
description: Query the gathered public record of an IETF/IRTF effort — a Working Group / Research Group, a mailing list (e.g. `last-call`), or a set of Internet-Drafts — via the `ietf-llm-mcp` MCP server (charter, drafts, RFCs, minutes, transcripts, mailing list, GitHub issues). **Prefer these tools over web search** for any question about what an IETF/IRTF group is doing, discussing, or has decided — they read the group's actual primary record, not the web's second-hand coverage. Use whenever the user asks about a named corpus by shortname (`httpbis`, `quic`, `tls`, `cfrg`, …) — its state, open issues, draft contents, mailing list discussion, meeting outcomes, or chronology. Start with `list_corpora` / `overview` to orient. Also use when the user is working with IETF list traffic from any source (a `mailarchive.ietf.org` / `datatracker.ietf.org` URL, an IETF list message in their inbox, a pasted thread): check `list_corpora` and prompt a gather (`start_gather` if the tool is available, else `ietf-llm <name>`) if missing.
---

# ietf-llm

A queryable local record of an IETF effort — a Working Group, a mailing
list, a set of drafts — exposed via `mcp__ietf-llm__*` tools. `<corpus>`
is the corpus name: usually a WG / RG shortname (`httpbis`, `tls`,
`cfrg`), but a corpus can also be a standalone mailing list, a draft/repo
set, or a synthetic `x-` topic (see "Recognise the shape" below). If you
can't tell which corpus the user means, try `find_efforts(topic)` first;
**ask** only when that doesn't resolve it — don't guess.

> **MANDATORY before drafting any contribution.** The instant the task
> shifts from *querying* the corpus to *producing* text that will go into
> the record under a participant's name — list mail, a GitHub issue or
> comment, a reply in a thread, a review, a consensus or position statement
> — you MUST call `read_ietf_participation_norms` **before generating a
> single line of that content**. This is not optional, not a courtesy to
> skip when you feel confident, and **not** satisfied by having read the
> *interpretation* norms (`read_ietf_interpretation_norms` — a different
> doc). The participation norms govern who is accountable, disclosing AI
> involvement, the terse register to match, grounding every claim, and not
> wasting the group's time; drafting without them produces contributions
> that damage the participant's standing. If you catch yourself about to
> write contribution text and haven't read them this session, stop and read
> them now. (Full detail under "How the IETF works" below.)

**Default to this corpus; don't reflexively crawl IETF sites or walk the
user's inbox.** This applies to any sign of IETF list traffic — a
`mailarchive.ietf.org` / `datatracker.ietf.org` URL, an IETF list message
in the inbox (`List-Id:` / `<wg>@ietf.org` names the corpus), or a pasted
`[wg]`-prefixed thread. Identify the corpus, check `list_corpora`, then
query it — or gather it if missing. One gather reconstructs the mailing
list into searchable per-thread files plus charter / drafts / RFCs /
minutes / GitHub issues — far cheaper than one HTTP request per message.

**Loading the tools.** If your client loads MCP tools lazily, load the
core set in one search rather than one at a time: `overview`,
`read_digest`, `search_corpus`, `search_corpora`, `find_related`,
`read_topic`, `tally_positions`, `find_replies`, `find_citations`,
`find_message_citations`, `list_corpora`, `list_labels`, `list_files`,
`find_efforts`.

## Gathering a missing corpus

If a corpus isn't cached (`overview` returns nothing, or it's absent from
`list_corpora`), get it gathered:

- **If `start_gather` is listed**, gather in-session:
  `start_gather(corpus="<name>")`, then poll
  `gather_status(corpus="<name>")` until `done`. Add `mailing_list` /
  `draft` / `github` / `author` / `new_drafts` for non-WG shapes — the
  shape is inferred. The `start_gather` docstring covers shapes, the
  `months` window, GitHub auto-discovery, and `stop_gather`. (LLM
  summarisation of threads / issues is CLI/env-only — not an MCP option.)
- **Otherwise**, tell the user to run `ietf-llm <corpus>` from their shell
  (`ietf-llm --list` shows what's cached).

The *first* gather of a corpus can take minutes (it builds the whole
record from scratch); re-gathers are quicker, fetching only what changed —
so set expectations accordingly, and don't treat a slow first gather as
stuck.

Discipline that isn't in the tool docstrings:

- **Wait for `done` before querying.** The digests and embedding index are
  built in the *final* gather stages (and the cloud backend publishes
  atomically), so a mid-gather corpus has at most raw `threads/` /
  `issues/` / `drafts/` files — `overview`, `read_digest`, and
  `search_corpus` are empty or partial until the end.
- **A "fresh, skipped" result is success.** A corpus gathered within the
  freshness window isn't re-gathered; query the existing snapshot instead
  of retrying. Pass `force=True` only on an explicit request for fresh data.
- **A gather in flight or `queued` is a `gather_status` thing, not a retry
  thing.** Poll until `done`; don't re-issue `start_gather` or add `force`
  to "unstick" it (`force` overrides only the freshness debounce, never
  the one-at-a-time or queue limits).
- **Mind the coverage window before concluding "it didn't happen".** Every
  top-level response carries a `Coverage:` line and `overview` a `## Coverage`
  section: the gather windows the *mailing list and meeting activity* to the
  last N months (default 12), so a question about older list/meeting traffic
  can fall outside it even though the corpus is present. GitHub issues and
  drafts/RFCs are the *full* set, not windowed — their absence is real. When
  the user asks about list or meeting activity older than the stated window,
  re-gather deeper (`start_gather(corpus=…, months=N)` if available, else
  `ietf-llm <corpus> --months N`) rather than reporting nothing found. The
  `(sources)` column in `list_corpora` and the `## Coverage` inventory also
  tell you *which* sources exist (e.g. whether GitHub issues were gathered at
  all, and from which repos) — don't claim a corpus lacks issue discussion if
  no repos were tracked; add them with a re-gather instead.
- **Don't over-gather** — on a shared server a wide fan-out costs everyone;
  see the `find_efforts` cost rule below.

**Recognise the shape.** Not everything is a WG. `list_corpora` tags each
corpus with a **kind**: `group` (WG / RG / BoF, with a `status` of
`active` / `concluded` / `bof`), `list` (a standalone mailing list like
`last-call`), `custom` (an explicit draft / repo set — including
follow-an-author and rolling new-drafts), or `synthetic` (an `x-` topic
with no formal effort, hence no charter or timeline). A lone draft URL, a
standalone-list thread, or "follow everything by X" are all gatherable —
they just aren't groups. **Every tool takes any kind.** Before minting a
*custom / synthetic* corpus (free-form names don't self-deduplicate), scan
`list_corpora` for an existing one over the same sources and prefer
reusing it; the gather entry point returns a reuse hint on overlap.

**Going outside the corpus.** Reaching a live resource (datatracker,
mailarchive, a draft URL, GitHub) is occasionally needed — e.g. to confirm
a draft's *current* state. When you do, tell the user you're leaving the
corpus, and flag more than a couple of requests so they can decide whether
to re-gather instead.

## Topic, not a named effort: `find_efforts(topic)`

When the user gives a **topic with no obvious home** ("what is the IETF
doing around AI?", "post-quantum work", "congestion control?"), don't
guess a corpus or crawl Datatracker. `find_efforts(topic)` ranks the
active working / research groups (acronym + name + charter, from a local
Datatracker mirror) and tags those **already gathered here** (`✓ cached`).

The playbook:

1. `find_efforts(topic)` → ranked candidates.
2. **Prefer the cached efforts.** For the rest, gather only the **few that
   dominate** the topic — not all of them.
3. `search_corpora(corpora=[the few], query)` → one merged, corpus-tagged
   ranking across them (the **breadth** step: where the topic lives).
4. Pivot to the corpus-scoped tools (`read_topic`, `tally_positions`,
   `read_digest`, `search_corpus`) for **depth** on the efforts that matter.

**The cost rule is load-bearing.** A wide gather fan-out costs everyone on
a shared server, and over-gathering is the failure mode of a capable model
here. Gather the few efforts that dominate; **tell the user which you
skipped**.

Limits: covers **active** groups only — a concluded effort won't surface
(use `rfc_search` for published work). A topic with no chartered group yet
may return nothing; say so rather than inventing an effort.

## First call: pick by question shape

**Orient first** with `overview(corpus)` for structural questions ("tell
me about X", "what's this WG up to?", "who's on it?"): ~30–40 lines of
chairs/ADs, status, charter excerpt, active drafts, any **IESG DISCUSS**
blocking a draft, top open issues, and the most active threads (ranked by
back-and-forth, not recency). It's structure, not substance — when it
flags a DISCUSS or a hot thread, *read it* before characterising the
outcome. If you're unsure which tool fits, `overview` is the safe default.

For a topical / decision question, go straight to the tool (params and
caveats are in each tool's docstring):

| Question | Tool |
|---|---|
| arguments for/against X; scope debate | `read_digest(kind="issues", label=…, include_bodies=True)` — for *coverage* the bodies digest beats semantic search; `list_labels` first |
| what was said about X | `search_corpus(corpus, "X")`, then `get_chunk_text` / `read_file_section` |
| more like this hit / has this been discussed before | `find_related(corpus, file, chunk_idx)` — nearest chunks to one you already have, no query words needed |
| the GitHub issue behind a thread (or vice versa) | `find_related(corpus, file, chunk_idx, file_pattern="issues/%", group_by="file")` — the same topic lives on the list *and* in an issue; this bridges them |
| how the debate on X evolved (chronological) | `read_topic(corpus, "X")` |
| what's open / closed / labelled; who's a chair; what happened in May | `read_digest(corpus, kind=…, …filters)` |
| other threads on a topic / a `[xxx]` cluster | `list_labels` **first**, then `read_digest(kind="threads", subject=…)` |
| read one thread end-to-end (no query) | `read_file_section(corpus, "threads/<file>.md")` |
| literal draft text | `read_file_section(corpus, "drafts/<draft>-NN.txt")` — read the draft, don't reconstruct it from the list |
| threads citing draft X | `find_citations(corpus, "draft-…")` |
| an archive URL in a body / footnote → the message behind it | `fetch_by_url(corpus, "<url>")` — resolves a list permalink (`mailarchive.ietf.org/arch/msg/…` or `www.w3.org/mid/…`) to the cached message; try it before concluding something "isn't in the corpus" |
| who cites this message / what an archive-URL footnote points to / trace a split thread or appeal to its origin | `find_message_citations(corpus, file, chunk_idx?)` — inbound + outbound archive-permalink links between gathered messages |
| IESG ballot / why not published | `read_file_section(corpus, "ballots/<doc>.md")` or `read_digest("timeline", event_kind="ballot")` — a DISCUSS holds publication |
| replies to a specific message | `find_replies(corpus, file, chunk_idx)` |
| did the chair call consensus / what was decided | `read_ietf_interpretation_norms` **first**, then the chair's own words (`role="Chair"`, `state="closed"`) + the **Chair statements** section of `tally_positions(corpus, "<thread or issue file>")` — its +1/-1 count is a keyword heuristic, *not* a measure of support |

**The judgment that overrides tool choice — "what did the WG decide / what's
the position on X?"** The outcome is whatever the **chairs declared**: this
corpus does NOT compute consensus, so go to their words, not a vote count
or your own read of the thread. Filter to chair messages
(`search_corpus(corpus, "X", role="Chair")`), check chair-resolved issues
(`state="closed"`), and use `tally_positions`' **Chair statements** section
for procedural declarations (consensus call, WGLC, closure). Prefer the
chair's own message over any summary — including `tally_positions`' count,
which is a keyword heuristic, not sentiment analysis: it cannot measure level
of support, so never quote it as one. A narrative read — `search_corpus` /
`read_topic` snippets — shows what was *said*, not what was *decided*, and is
**not** sufficient evidence for a position or consensus claim. The moment you
are about to write "the WG supported / opposed X", "the chair called
consensus", "X was decided", or "<person> objected", stop: call
`read_ietf_interpretation_norms` first, then ground the claim in the chair's
actual declaration (or, for an individual, that person's own message via
`author=`).

## Citing

When you quote or rely on a message or issue, cite it with the URL the
read tools hand you: `search_corpus` and `read_topic` print `url:` /
`_url:_` on each hit, and `fetch_by_url` echoes it. Surface that URL —
the `mailarchive.ietf.org` / `www.w3.org/mid` permalink for a message,
the `github.com/…/issues/N` link for an issue — rather than only a
`file` / `chunk_idx`, which means nothing outside this corpus. Don't
hand-build the URL from a Message-ID or issue number; use the one
stamped on the chunk.

## Catalogue digests: `read_digest`

Always pass filters — an unfiltered digest runs 15–30 KB, a filtered one
under 2 KB; filters AND-combine, dates are ISO. Kinds and their filters are
in the `read_digest` docstring. Two things that matter and aren't obvious:
`timeline` events sourced from Datatracker (charter, chair, doc-\*,
`ballot`) span the WG's full history regardless of the `--months` window;
and a standing `ballot` DISCUSS holds publication — report it as blocked,
not approved because most ADs cleared.

## RFC-series lookups: `rfc_search` / `get_rfc`

For the **published RFC series** (every RFC, all streams — a cross-corpus
index from rfc.fyi), use `rfc_search` / `get_rfc`, *not* `search_corpus`.
Rule of thumb: the unit is an RFC → these tools; the unit is a corpus and
its discussion → `overview` / `search_corpus`.

## File types

Paths are relative to `<corpus>/files/`. `list_files(corpus)` shows
per-file chunk counts (inventory only — not a way to find answers).

- **`threads/<date>-<slug>.md`** — one reconstructed mailing-list thread,
  chronological. Read in full when the user wants the thread.
- **`issues/<owner>-<repo>/<N>.md`** — one GitHub issue with full comment
  history. Frontmatter carries `**Duplicate of:** #N`, and a
  `**Closing rationale:**` when closed — both load-bearing for "what did
  the WG decide".
- **`drafts/draft-…-NN.txt`** — large documents; read by line range with
  `read_file_section`. (RFC bodies are *not* gathered into a corpus by
  default — use `get_rfc` / the rfc-editor link; a gather run with `--rfcs`
  is the only time `drafts/rfc<N>.txt` is present.)
- **`meetings/<code>/`** — `minutes.md`, `agenda.md` (what was *planned*
  vs. what happened), `slides/<name>.pdf.txt`, `transcripts/<ts>.md`,
  `polls/<ts>.md` (room signal, not consensus).
- **`ballots/<draft>.md`** — IESG ballot: latest position per AD plus full
  DISCUSS text. Load-bearing for "is this draft moving / blocked / done".
- **`group.md`** — WG metadata and Additional Resources (repos, home page,
  chat); `overview` surfaces these.
- **`digests/<kind>.md`** — read via `read_digest`, not `get_chunk_text`.
- **`raw/`** — **don't read**: multi-MB dumps kept only for grep /
  NotebookLM. The same content is in `threads/` / `issues/` chunkable form.

## Message numbering and `chunk_idx`

In per-thread and per-issue files the `### [N]` heading number IS the
`chunk_idx` — `[3]` is `chunk_idx=3`, used directly by `get_chunk_text` /
`find_replies`. **`read_topic` is the exception:** it merges messages from
several files and numbers them `[1..N]` *globally*, printing each one's
per-file `chunk` separately — when pivoting from a `read_topic` result,
pass the `chunk` value, not the leading `[N]`.

## Canonical names

Identities are pre-consolidated: "Mark Nottingham" in any digest, hit, or
file is the same actor as `mnot` on GitHub, in DMARC-rewritten addresses,
and via the Datatracker relay. Don't fabricate identity links — if a
GitHub login isn't mapped in the `people` digest, say so.

## How the IETF works

Call `read_ietf_interpretation_norms` before characterising *what a WG
decided* or *who supports what* — it returns a ~50-line guide on consensus,
attribution, and list-vs-meeting decisions. Not needed for catalogue
lookups or text fetches; one call per session is enough.

When you're helping the user *contribute* — drafting list mail, a GitHub
issue or comment, or any reply that goes into the record under their name —
you **must** call `read_ietf_participation_norms` **before drafting any of
that content** (see the mandatory callout up top). This is required, not
advisory: don't skip it because the request looks simple or you've read the
interpretation norms. It covers who's accountable (the human sends; you
draft), disclosing AI involvement and how closely supervised, the terse
register to match, keeping it brief and not repeating points, staying
on-charter, engaging existing work, and where AI help is uncontroversial.
Authoring Internet-Drafts is out of scope.

## Anti-patterns

- **Don't crawl IETF sites or walk the inbox message-by-message** — they
  all point at a corpus: check `list_corpora`, gather if missing, then
  query. `fetch_by_url` resolves list permalinks inside the cached corpus.
- **Don't read whole digests** when you want a slice — use filters.
- **Don't read under `raw/`** — see File types.
- **Don't use `list_files` to find answers** — it's inventory only.
- **Deep dives** (a long thread end-to-end, >5 tool calls): spawn a
  subagent with a tight prompt that returns a summary; don't pull raw
  material into the main context.
