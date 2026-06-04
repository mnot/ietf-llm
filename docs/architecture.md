# Architecture

A short read-this-first for anyone looking at the codebase. Covers the
shape of the system, where state lives, what each module does, and the
decisions that aren't obvious from the code.

## Elevator pitch

`ietf-llm` maintains a local, LLM-queryable corpus of an IETF effort's
public record — a Working Group, a mailing list, a set of drafts. One
CLI gathers and indexes; three consumers
read what was gathered — a semantic search CLI, an MCP server, and a
NotebookLM exporter.

The cache is the single source of truth. Everything writes to it once
and reads from it forever.

## The four CLIs

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | gather / refresh a corpus, build digests, build embedding index | network | cache |
| `ietf-llm-search` | semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | expose the cache to MCP clients (Claude, Codex, etc.) | cache | stdio / HTTP (MCP) |
| `ietf-llm-export` | mirror to local dir, or push to NotebookLM Enterprise | cache | local dir / NotebookLM |

This is the load-bearing shape of the project: **one writer to the
cache, three independent readers.** Any consumer can come and go
without touching the gather pipeline. Conversely, the gather pipeline
doesn't know or care which consumers are downstream.

`ietf-llm --list` prints the cached WGs; `ietf-llm --completion <shell>`
prints a shell tab-completion script.

## Corpus names and kinds

A corpus is not necessarily a Working Group — it's a named bundle of
sources, and "a WG" is one preset. `<wg>` is a corpus name (the code
keeps the historical `wg` parameter name). `_gather_one` classifies it
once, in `__main__._resolve_corpus_shape`, into one of four **kinds**:

- **group** — the name resolves to a Datatracker group (WG / IRTF RG /
  editorial WG / BoF). Gets the full auto-sourced pipeline: charter,
  group metadata, meetings, documents, transcripts, datatracker roles,
  and the timeline group / ballot / doc-event queries.
- **list** — not a group, but the name resolves as a list archived at
  mailarchive.ietf.org (IETF / IRTF / RFC-Editor — `last-call`,
  `irtf-discuss`, `rfc-interest`). Gathers only that list, synced from
  the IETF IMAP mirror; the address domain, if given, is stripped.
- **custom** — not a group; content comes from explicit `--draft` /
  `--mailing-list` / `--github` (the name is a label).
- **synthetic** — an `x-` prefixed name (`x-webbotauth`); like custom
  but explicitly skips even the group lookup. `utils.is_synthetic_wg()`
  is the predicate; the prefix is the only signal.

Resolution **precedence** (first match wins): `x-` synthetic →
generative flags (`--new-drafts` / `--author`, which make the name a
label) → Datatracker group → mailarchive list → typo. So a name that is
both a WG and a list resolves as the **group** — no loss, since a group
corpus already auto-discovers that group's own list.

There is **no `--list-only` flag** — the kind is inferred. A name that
is neither a group, a known list, nor configured with sources is
rejected as a likely typo rather than producing an empty corpus.

**Generative source flags** (`--new-drafts`, `--author`) populate a
custom corpus dynamically — a rolling `-00` window from the submission
API, or a person's authored drafts from the documentauthor table.
They short-circuit shape inference to `custom` (the name is a label, no
group lookup) and persist, so a bare re-run re-evaluates the source.
Explicit sources (`--draft` / `--mailing-list` / `--github`) compose
onto any kind. `--add-mentioned-drafts` is a *derived* source: it
reuses the citation scan to pull drafts the corpus cites but lacks,
persisting the accumulated set (`mentioned_drafts`) so it stays sticky
and is retained through the new-drafts prune.

The gather pipeline gates Datatracker-sourced steps on a single
`group_backed` boolean (true only for the `group` kind). `corpus.py`
derives `(kind, status)` from on-disk artifacts for `ietf-llm --list`
and the MCP `list_corpora` tool — identically, so they can't
drift; `status` is the cached group state (`active` / `concluded` /
`bof`). `corpus.describe()` adds a brief **subject** line for the same
two surfaces — the group's name (from `group.md`), the list it follows,
or the tracked author (the resolved name persisted as `author_name`) —
so a consumer can tell what an opaquely-named corpus is about without
opening it. Both are network-free; older caches lacking the stored name
degrade to an empty subject until the next gather.

## Cache layout

Everything the gather produces lands under `~/.cache/ietf-llm/`. The
layout is the responsibility of `paths.py` — the single source of
truth for where files live. Don't hardcode paths elsewhere; call the
helpers. The root is relocatable via `IETF_LLM_CACHE_DIR` (and
`IETF_LLM_CONFIG_DIR`), and the per-WG `embeddings.db` files can live on
a separate, faster volume via `IETF_LLM_INDEX_DIR` — so a deployment can
park the hot index on tmpfs while the corpus comes from elsewhere.

```
~/.cache/ietf-llm/
├── <wg>/                                 # one dir per WG / corpus
│   ├── files/                            # the corpus consumers read
│   │   ├── charter.txt
│   │   ├── group.md                      # name, status, parent area, Additional Resources
│   │   ├── digests/                      # index.md issues.md threads.md
│   │   │                                 # people.md timeline.md citations.md
│   │   ├── drafts/                       # draft-*.txt, rfc*.txt
│   │   ├── meetings/<code>/
│   │   │   ├── minutes.md
│   │   │   ├── agenda.md
│   │   │   ├── slides/<slug>.pdf(.txt)
│   │   │   ├── transcripts/<YYYYMMDDHHmm>.md
│   │   │   └── polls/<YYYYMMDDHHmm>.md
│   │   │   └── …  (meetings/_orphans/ holds unmatched transcripts)
│   │   ├── threads/<date>-<slug>.md       # one reconstructed thread each
│   │   ├── issues/<repo-slug>/<N>.md      # one GitHub issue each
│   │   ├── ballots/<draft-name>.md        # IESG ballot positions per draft
│   │   ├── github/<repo-slug>.json        # raw archive (internal; not exported)
│   │   └── raw/                           # NOT indexed; grep / NotebookLM only
│   │       ├── mail-archive-<YYYY>.txt
│   │       └── github-<repo-slug>.txt
│   ├── embeddings.db                      # per-WG semantic index
│   ├── materials.json                     # doc-name → rev last fetched (rev-gating)
│   ├── documents.json                     # draft-name → {expires, state} (overview split + embed-skip)
│   ├── last-gathered                      # ISO-8601 sentinel (freshness)
│   └── gather-metrics.json                # last run's upstream HTTP load (egress metrics)
│
├── imap-cache/<wg>/<list>/<uid>.eml       # raw fetched messages
├── transcripts-repo/                      # shallow clone of ietf-minutes-data
├── _rfc/                                   # cross-corpus RFC-series index (singleton)
│   ├── rfcs.json                          #   per-RFC metadata (mirrored from rfc.fyi)
│   ├── refs.json                          #   normative / informative references
│   ├── tags.json                          #   curated rfc.fyi collections
│   └── *.etag                             #   per-file ETag sidecars (conditional GET)
├── _catalog/                               # cross-corpus active-effort catalog (singleton)
│   ├── catalog.json                       #   derived slim effort records (find_efforts reads)
│   ├── raw-active.json / raw-bof.json     #   raw Datatracker group slices (revalidation)
│   └── *.etag                             #   per-file ETag sidecars (conditional GET)
├── .http-cache.json                       # shared ETag store (conditional GET)
└── _github-users.json                     # shared login → name / company cache
```

Key invariants:

- **`<wg>/files/` is what consumers read.** Anything an MCP tool, the
  search CLI, or the exporter can see lives there. Everything else in
  the cache is intermediate state owned by the gather pipeline.
- **The mailing list is materialised in two shapes.** `raw/mail-archive-YYYY.txt`
  is a flat year-dump (kept for grep / NotebookLM upload, excluded
  from the embedding index). `threads/<date>-<slug>.md` is one file
  per reconstructed thread, built from RFC 5322 In-Reply-To /
  References headers with a normalised-subject safety net, quoted runs
  collapsed, an outline at the top, and per-message section headers
  rendered in **UTC** (so the chunker's date re-parse sorts correctly).
  The thread files are what an LLM should read; the year files are for
  humans / external tools.
- **Per-issue files mirror per-thread files.** `issues/<repo>/<N>.md`
  is one GitHub issue with full comment history, same shape as a
  thread file (frontmatter carries duplicate-of and closing-rationale).
- **Identities are consolidated up front.** `people.Registry` scans
  mailing-list From headers, GitHub author logins (+ the shared
  `_github-users.json` name/company cache), Datatracker role
  assignments, and draft Authors' Addresses sections, and merges the
  surface forms of one actor (DMARC-rewritten variants, relay
  addresses, multiple emails, GitHub logins matching an email
  local-part) into one canonical `Person`. GitHub logins are further
  linked to identities via each person's Datatracker `github_username`
  profile resource (exact, by verified email), then by display name
  (`people_linking`). A Person also carries
  **affiliations** (keyed by source: `draft:<doc>` and `github`) and
  the set of **email domains** seen — distinct fields, because email
  domain ≠ affiliation. The `digests/people.md` digest leads with
  leadership and document-author tables. Threads, issues, and the raw
  github text all render authorship with canonical names.
- **`digests/*.md` are deterministic, regenerated every gather.**
  `index`, `issues`, `threads`, `people`, `timeline`, `citations`.
  Read them via the MCP `read_digest` / `overview` tools, not raw.
  `citations.md` is the draft → citing-thread/issue cross-reference.
- **`ballots/<draft>.md`** holds the current IESG ballot (latest
  position per AD, DISCUSS text inline) for drafts with ballot
  activity in the `--months` window.
- **`embeddings.db` is per-WG.** `meta` records the embedding model id,
  the chunker version, and the vector dimension; a change to any of the
  three forces a rebuild, and the model id is read back at query time to
  resolve the same backend. Chunks are sized to the model's token budget
  — a long thread/issue section is split into several sub-chunks rather
  than truncated. The `chunks` table carries `start_line`/`end_line`,
  `chunk_date`, `labels`, `state`, `url`, `duplicate_of`,
  `closing_rationale` for faceted search. Schema is versioned; `_open_db`
  migrates older DBs forward via ALTER TABLE. Not every cached file is
  embedded: `raw/`, `github/`, digests, and PDFs are excluded, and a
  draft's revision stack is skipped once its Datatracker `state`
  (`documents.json`) is `rfc` (published — the RFC is canonical) or
  `repl` (replaced — content lives in its successor). Those revisions
  stay on disk for reading/citing; only embedding is gated. Active /
  expired drafts and the RFC texts themselves are embedded.
- **`imap-cache/<wg>/<list>/`** is the only place holding raw `.eml`
  files. Thread reconstruction walks that tree (two levels — one
  subdir per list, since a WG can follow several).
- **`last-gathered`** is an ISO-8601 sentinel `freshness.py` writes at
  the end of each gather; consumers surface staleness from it.
- **`gather-metrics.json`** records the upstream HTTP load of the last
  run — request counts (transferred / revalidated / error), bytes, a
  per-host breakdown, and a top-N of URL patterns. `http_metrics.py`
  accumulates it at the two egress chokepoints (`datatracker._get_json`
  and `utils.fetch_resource`) and the CLI prints a one-line summary at
  the end of a gather. Beside the sentinel (one level above `files/`),
  so it is neither indexed nor exported.
- **`_rfc/` is a cross-corpus singleton, not a corpus.** It mirrors the
  whole published RFC series from rfc.fyi (three JSON blobs), refreshed
  once per gather run after the per-corpus work, TTL-guarded and
  best-effort (`gather/rfcs.py`). The leading underscore keeps it out of
  `list_corpora` / `ietf-llm --list`, which enumerate real corpora. The
  `rfc_search` / `get_rfc` tools read it; it is not embedded.
- **`_catalog/` is the matching singleton for active efforts.** It
  mirrors the active (and BoF) slice of the Datatracker group list,
  refreshed beside `_rfc/` in tail housekeeping with the same TTL / ETag
  / never-raises discipline (`gather/catalog.py`). Unlike the RFC mirror
  its reader-facing file is *derived*: the raw source slices
  (`raw-active.json` / `raw-bof.json`) are kept for revalidation, then
  projected to the slim `catalog.json` record list the reader wants. The
  leading underscore keeps it out of the corpus enumerations; the
  `find_efforts` tool reads it; it is not embedded.

## Config layout

Per-WG, per-tool persistent flags live under `~/.config/ietf-llm/`:

```
~/.config/ietf-llm/
├── config.json                             # global service settings (see below)
├── client_secrets.json                     # GCP OAuth (NotebookLM only)
├── token.json                              # cached OAuth token
└── <wg>/
    ├── gather.json                         # per-WG ietf-llm flags
    └── export.json                         # per-WG ietf-llm-export flags
```

The split into two scoped files is deliberate: the gather and export
tools have non-overlapping flag sets, and a user reasoning about "how
is this WG configured" should read one or the other without seeing
irrelevant settings. `ietf-llm <wg> --clear-config` wipes the whole
`<wg>/` config dir.

`config.json` is the **global** scope (`config.merge_global`), for
settings that are properties of the tool or deployment rather than of a
corpus: the embedding model, embed on/off, and the summariser. As of
0.8.0 these resolve `env > CLI > global config > default` and are no
longer persisted per-WG (`_migrate_global_keys` strips legacy per-WG
values on the next gather). Secrets — embedding tokens, etc. — come from
the environment only and are never written here. See the *Embedding
backends* doc (`models.md`) for the full variable list.

## Data flow

```
   Datatracker ──┐                ┌─────────────────────────────┐
   IMAP archive ─┤                │  ~/.cache/ietf-llm/<wg>/     │
   GitHub API   ─┼──> ietf-llm ──>│   files/  (corpus + digests) │
   minutes repo ─┤   (gather)     │   embeddings.db              │
   (drafts, …)  ─┘                └──────────────┬──────────────┘
                                                 │
                ┌────────────────────────────────┼──────────────────────────┐
                ▼                                ▼                          ▼
        ietf-llm-search               ietf-llm-mcp                 ietf-llm-export
        (CLI, stdout)                 (MCP: stdio / HTTP)          (local dir / NotebookLM)
```

Once gathered, the cache is consumer-agnostic. Adding a new consumer
is a matter of writing code that reads the cache; no change to gather.

One side-channel sits beside this: every gather run also refreshes the
cross-corpus RFC-series index from rfc.fyi into `_rfc/` (TTL-guarded,
best-effort), which the MCP `rfc_search` / `get_rfc` tools read. It is a
singleton mirror, not part of any corpus, so it stays outside the
per-`<wg>` writer/reader flow above.

## Package layout

```
ietf_llm/
├── __main__.py             # `ietf-llm` (gather): argparse, persisted config,
│                           # orchestration: charter → group info → meetings →
│                           # mailing list →
│                           # transcripts → drafts → github → registry → per-thread
│                           # / per-issue files → citations → digests → embed.
│                           # Also --list / --completion / --install-claude-skill.
├── export_cli.py / export.py   # `ietf-llm-export` entry point + mirror/NotebookLM logic
├── search_cli.py           # `ietf-llm-search` entry point
├── mcp_server.py           # `ietf-llm-mcp` (FastMCP stdio server + tools)
├── skill_install.py        # --install-claude-skill + pristine-only auto-update on CLI gathers
├── config.py               # generic per-WG, per-scope JSON config (merge/persist)
├── corpus.py               # corpus kind/status + subject line (group/list/custom/synthetic)
├── paths.py                # cache-layout single source of truth; meeting_label()
├── freshness.py            # last-gathered sentinel + staleness warnings
├── http_metrics.py         # per-gather upstream HTTP egress accounting (thread-local)
├── people.py               # actor/identity registry (roles, affiliations, domains)
├── people_linking.py       # attach GitHub logins to identities (Datatracker, then name)
├── positions.py            # heuristic position / poll / chair-statement extraction
├── notebooklm.py           # Google OAuth + Discovery Engine API
├── text.py                 # generic text helpers (subject norm, date, addr)
├── utils.py                # log(), Verbosity/LogLevel, cache/config dirs, HTTP
│                           # defaults, group metadata via API (type/title/list),
│                           # is_synthetic_wg, cached_wg_names,
│                           # write_if_changed, argcomplete helpers
├── oai_compat.py           # shared OpenAI-compatible HTTP plumbing (auth headers,
│                           # retry + Retry-After) for the remote embed / summarise backends
├── rfcs.py                 # cross-corpus RFC-series reader (rfc_search / get_rfc);
│                           # reads the _rfc/ singleton mirrored from rfc.fyi
├── catalog.py              # cross-corpus active-effort reader (find_efforts);
│                           # ranks the _catalog/ singleton by topic, tags cached efforts
├── gather_runner.py        # in-session gather: runs the __main__ pipeline off-thread,
│                           # writes gather-status.json (start_gather / gather_status)
├── gather_stages.py        # stage_plan: canonical gather stage order (shared CLI ↔ runner)
├── _stdio_transport.py     # threaded-writer stdio transport (sidesteps upstream blocking write)
├── _debug_log.py           # per-request telemetry ring buffer (IETF_LLM_DEBUG_LOG / get_session_log)
├── data/skill/SKILL.md     # bundled Claude skill (also fed to MCP `instructions`)
├── data/skill/IETF.md      # interpretive norms, served on demand via read_ietf_norms
│
├── gather/                 # content acquisition + per-source post-processing
│   ├── charter.py              # charter text artifact (rev from doc API)
│   ├── group_info.py           # group.md: name / status / area / Additional Resources
│   ├── drafts.py               # WG drafts + RFCs via doc API; --draft extras
│   ├── recent_drafts.py        # --new-drafts: -00 submissions in the window
│   ├── author.py               # --author: a person's authored drafts
│   ├── meetings.py             # minutes/agenda/slides via meeting API; clustering
│   ├── transcripts.py          # ietf-minutes-data repo; match to meeting clusters
│   ├── transcript_context.py   # prepend meeting-context header to transcripts
│   ├── mbox.py                 # IMAP fetch + per-year .txt; --mailing-list extras
│   ├── mail_threads.py         # reconstruct per-thread .md files
│   ├── github.py               # archive.json (gh-pages) or REST API
│   ├── github_users.py         # resolve logins → real name + company
│   ├── issue_files.py          # per-issue .md files
│   ├── datatracker.py          # roles + paginated document listing via JSON API
│   ├── json_store.py           # tolerant read + atomic write for the JSON manifests below
│   ├── materials_manifest.py   # materials.json: doc-name → rev last fetched (rev-gating)
│   ├── documents_manifest.py   # documents.json: draft-name → {expires, state} (overview + embed-skip)
│   ├── _mirror.py              # shared singleton-mirror plumbing (TTL / conditional GET / sidecars)
│   ├── rfcs.py                 # ensure_rfc_index: mirror the rfc.fyi RFC-series JSON → _rfc/
│   ├── catalog.py              # ensure_catalog_index: mirror the Datatracker group list → _catalog/
│   ├── datatracker_history.py  # governance / doc-lifecycle timeline events
│   ├── datatracker_github.py   # github_username profile resources → person (by email)
│   ├── draft_authors.py        # parse Authors' Addresses (name + organization)
│   ├── ballots.py              # IESG ballot positions (scoped to --months)
│   ├── citations.py            # draft → citing thread/issue cross-reference
│   ├── pdf_extract.py          # extract text from slide PDFs
│   └── session_polls.py        # session polls (polls doctype) → JSON tallies
│
├── digest/                 # corpus-level digest builders + consumers
│   ├── __init__.py             # generate_digests() + re-exports
│   ├── events.py               # shared Event dataclass (gather ↔ digest seam)
│   ├── helpers.py              # state case-folding, size formatting, re-exports
│   ├── summarizer.py           # optional LLM-backed one-liner wrapper (llm lib or remote)
│   ├── remote_summarizer.py    # openai-summarize/ remote chat-completions backend
│   ├── issues.py / threads.py / index.py   # per-kind digest builders
│   ├── timeline.py             # chronological event log (incl. Datatracker, ballots)
│   ├── overview.py             # one-call composed summary (+ charter excerpt,
│   │                           # citation counts, freshness line)
│   └── query.py                # filtered / paginated digest reads
│
└── embeddings/             # semantic search (split for legibility)
    ├── __init__.py             # public surface + re-exports
    ├── chunking.py             # per-message / per-issue / windowed chunkers; splits long sections
    ├── storage.py              # sqlite schema, vector packing, lookup
    ├── models.py               # embedding-model loading + process-level cache
    ├── snippet.py              # structure-aware snippet rendering for hits
    └── search.py               # build_index() and search()
```

## MCP tool surface

`mcp_server.py` registers each tool as a thin wrapper over a pure
`tool_*` function (so the logic is testable without MCP). Grouped by
job:

- **Orient:** `list_corpora`, `overview`, `list_labels`,
  `list_files`, `read_ietf_norms` (the bundled `IETF.md` interpretive
  norms — consensus, attribution, list-vs-meeting — served on demand so
  the always-on `instructions` field stays focused on tool routing).
- **Discover (topic-first):** `find_efforts(query)` ranks the active
  IETF/IRTF efforts by a free-text topic and tags each with whether it
  is already gathered here — the entry point for "what is the IETF doing
  around X?" when no corpus is named. It reads the `_catalog/` singleton,
  *not* a gathered corpus; v1 covers active groups only.
- **Catalogue:** `read_digest(kind=…, …filters)` over
  issues/threads/people/timeline/index. Beyond the per-kind filters,
  `sort="activity"` ranks threads/issues by message/comment count (heat,
  not recency) and `exclude_mechanical=True` drops routine timeline
  events (I-D Action publications, individual ballot positions) — the
  same signals the `overview` "most active threads" and folded
  "recent activity" sections reuse.
- **Search:** `search_corpus(query, …)` with `label`/`state`/`author`/
  `role`/`file_pattern`/`since`/`until`/`sort="date"`/`group_by="file"`/
  `snippet_chars`/`collapse_versions` facets (`collapse_versions`,
  default on, hides older draft revisions of a matched draft).
- **Narrative:** `read_topic` (full messages, chronological, across
  files; numbered globally, mechanical headers de-duplicated),
  `find_replies` (reply tree of one message), `tally_positions`
  (grounded support/oppose/poll count + chair-statements section),
  `find_citations` (threads citing a draft).
- **Pivot / read:** `get_chunk_text`, `get_chunks_batch`,
  `fetch_by_url` (resolves the `w3.org/mid` Archived-At permalinks and
  GitHub issue URLs the corpus actually stores), `read_file_section`.
- **RFC series (cross-corpus):** `rfc_search(query, …filters)` over the
  whole published RFC series and `get_rfc(number)` for one RFC's
  metadata + reference graph. These read the `_rfc/` singleton, *not* a
  gathered corpus — distinct from `search_corpus`, which is semantic
  search within one WG. A bare RFC number short-circuits to that RFC.
- **Diagnostics (gated):** `get_session_log` is registered **only** when
  `IETF_LLM_DEBUG_LOG=1` — it returns this process's per-request
  telemetry for investigating client-side stalls; with logging off it
  has nothing to return, so it is left out of the advertised tool list
  rather than shipped as a no-op.

Every wrapper offloads its blocking `tool_*` body to a worker thread
with a per-call deadline (`IETF_LLM_TOOL_TIMEOUT`, default 120s) so a
stuck call fails fast instead of hanging to the client ceiling, and
guards against an unknown corpus name (a read-only existence check, so a
typo neither creates a cache dir nor returns a hollow result).

The full SKILL.md guidance is also handed to compliant clients via the
MCP server's `instructions` field, so non-Claude harnesses get the same
routing rules without the Claude-specific skill install.

## Key design decisions

These are the ones worth knowing before you make changes.

### Cache is the only durable state

Every consumer reads from the cache. The cache is the contract. Adding
a consumer never requires touching gather; gather never has to know
who reads its output. This is the project's main architectural lever.

### Use the Datatracker API; do not scrape HTML

**When the data is available from the Datatracker REST API, the gather
layer MUST use it rather than parsing a rendered HTML page.** The API
([https://datatracker.ietf.org/api/](https://datatracker.ietf.org/api/),
browsable under `/api/v1/`) returns structured, stable fields and
canonical IDs; the HTML layout changes without notice and silent
scrape breakage ("gather suddenly returns nothing") is the failure
mode this rule exists to prevent.

Concretely, the gather layer reads from the API for:

- **Group metadata** — type (WG/RG), title, mailing-list address,
  state, and parent area — via `/api/v1/group/group/?acronym=<wg>`
  (`utils.fetch_group_object`), plus the "Additional Resources"
  (repos / home page / chat / alternate archives) from
  `/api/v1/group/groupextresource/` (`utils.get_group_resources`).
  The off-IETF mailing-list fallback (httpbis → `httpbisa`) reads the
  alternate-archive resource here.
- **Charter** — revision from the document API, then the published
  plain-text artifact at `www.ietf.org/charter/<doc>-<rev>.txt`
  (the datatracker doc page is HTML-only).
- **Documents** — WG drafts, RFCs, session polls — via
  `/api/v1/doc/document/?group__acronym=<wg>&type=…`, paginated through
  `datatracker.iter_group_documents`.
- **Meetings & materials** — sessions, dates, and per-session
  minutes/agenda/slides — via `/api/v1/meeting/session/?group__acronym=<wg>`
  and the linked meeting/material objects (`meetings.get_meeting_links`).
  Material *content* is fetched from its canonical URL
  (`/meeting/<n>/materials/<docname>`) with `Accept: text/markdown`,
  resolving to the latest rendered markdown / text / PDF.
- **Roles, ballots, governance events** — the `group/role`, `iesg`,
  and document-event endpoints (`datatracker.py`, `ballots.py`,
  `datatracker_history.py`).

`BeautifulSoup` survives only where there is no structured source:
`utils.clean_html` cleans the MCP `fetch_by_url` tool's arbitrary
user-supplied pages and the handful of older minutes authored directly
in HTML (served as a fragment when markdown is requested). New gather
code that reaches for an HTML page should first confirm the API can't
answer.

### Network is minimised three ways

Re-gathers should transfer as little as possible. The API exposes the
affordances; we use all three:

- **Conditional GET.** `datatracker._get_json` persists each endpoint's
  ETag (`.http-cache.json`) and revalidates with `If-None-Match`; an
  unchanged endpoint returns an empty 304 and we reuse the cached body.
  Covers the bulk of per-gather metadata (document lists, the meeting
  batch, roles). The materials *content* endpoint ignores conditional
  GET, so it is gated differently (below).
- **Batch filtering.** Meetings are resolved in one `id__in` call
  rather than one GET per session (`get_meeting_links`); document
  listings page through `meta.next`.
- **Revision-gated content.** Material content can't be conditionally
  fetched, but each material's document `rev` is cheap metadata. We
  record the rev last written per material (`<wg>/materials.json`) and
  re-fetch minutes/agenda content only when the rev changes — fixing
  the old skip-if-exists that froze minutes forever. Slides (large
  metadata, rarely revised) and polls (immutable) stay skip-if-exists.

### Writers are write-if-changed and atomic

`mail_threads`, `issue_files`, `ballots`, and the digest/minutes
writers regenerate content every gather but write a file only when its
bytes actually changed (`utils.write_if_changed`). A byte-identical
re-render leaves mtime untouched — load-bearing because the embedder
re-embeds any file whose mtime advanced. The mtime rule alone can't
catch a file that becomes *ineligible* without changing — a removed
thread/issue, or a draft that flips to `rfc`/`repl` and is now skipped
(see `embeddings.db` above) — so `build_index` opens with a prune: it drops chunks
for any indexed file no longer in the eligible set. That keeps stale
chunks from lingering and doubles as the migration path when the
eligibility rules change (an existing cache sheds the now-skipped
revisions on its next gather, no `--rebuild` needed).

Every-gather corpus writes also go through `utils.atomic_open` (temp +
`os.replace`), so the write is atomic — see the concurrency note below.

### Concurrency: gathers and servers can overlap

The corpus is shared mutable state: an MCP server may be answering
queries while a gather rewrites the same WG, and several gathers (or
servers) can run at once. The safety model:

- **The MCP server is read-only.** `search` only ever SELECTs;
  `build_index` (the sole writer) runs only from the gather CLI. So
  multiple servers never conflict — readers don't block readers.
- **Index DB.** Connections use WAL + a 30 s busy timeout
  (`storage._connect`), so an MCP query during a same-WG index rebuild
  reads the last committed snapshot instead of erroring with "database
  is locked". Concurrent builds of one corpus serialise on a per-corpus
  `file_lock`, and a build checkpoints the WAL (`TRUNCATE`) before close
  so the published `embeddings.db` is a self-contained object. A
  read-only replica opens with `IETF_LLM_INDEX_IMMUTABLE=1` (SQLite
  `immutable=1`) — the only way to read a WAL DB from a read-only mount,
  where the `-shm` sidecar otherwise can't be created. search() closes
  its connection on every return so a query never pins the WAL.
- **Corpus files.** Atomic writes (above) mean a reader sees the old
  bytes or the new, never a truncated file. Skip-if-exists files
  (drafts, RFCs, transcripts, slide text) are written once and not
  listed until indexed, so their first-write window isn't observable.
- **Shared single-clone / shared caches.** The one transcripts git
  clone is guarded by `utils.file_lock` (flock) so concurrent gathers
  serialise their clone/pull. The JSON side-caches
  (`.http-cache.json`, `materials.json`, `_github-users.json`,
  config) are written temp + rename; concurrent writers are
  last-writer-wins, which at worst costs a redundant re-fetch, never a
  corrupt file.

### `ietf-llm` (gather) has no `--update` flag

Re-running is idempotent: fetch what's missing, re-fetch what changed,
regenerate digests, incrementally update the embedding index. No
separate "update" mode because there's no separate "first run" mode.

### Interim sessions are clustered into one meeting

Datatracker lists each interim *session* as its own row. `meetings.py`
clusters interim rows whose dates are contiguous (≤ 1 day apart) into
one `MeetingCluster` keyed by its start date (`interim<YYYYMMDD>`);
materials merge into the canonical dir and interim transcripts (which
carry no meeting number) are matched to a cluster by date span instead
of orphaning. Numbered IETF meetings are never clustered.

### `ietf-llm-export` always does a full export

When the cache changes, the next export produces a complete fresh
output — no delta tracking. For NotebookLM the workflow is to create a
new notebook each update rather than merge into an existing one.

### The default embedding model is local; the backend is pluggable

`sentence-transformers/BAAI/bge-small-en-v1.5` is the default (~130 MB,
MPS-accelerated, no API key), but it lives behind the optional
`local-embeddings` extra (torch is not in the base install). The
`_get_embed_model` choke point dispatches on an id *prefix*:
`sentence-transformers/` constructs the local model;
`openai-embed/<model>` constructs a provider-neutral, network-backed
OpenAI-compatible (`/v1/embeddings`) client configured entirely from the
environment (`is_remote_embed_model` is the predicate); anything else
falls through to `llm`. The remote backend pulls no torch, so a serving
container can stay lean.

Whichever backend built an index, its id is recorded in the DB's `meta`
(alongside the chunker version and the vector *dimension*, recorded as
provenance), and `search` reads the id back to resolve the same backend.
Vectors are *not* portable across backends — even the "same" model isn't
bit-identical across runtimes — so the id prefixes never collide and a
dimension change forces a rebuild. See the *Embedding backends* doc
(`models.md`) for the variables.

### `--summarize` requires explicit setup; embedding doesn't

Embedding is on by default (opt out with `--no-embed`) and tolerates a
small local model fine; a bad summary is worse than none. Summarisation defers to whatever LLM the user configured via
the `llm` package; without one, we print setup help rather than limp
along. Deterministic digests ship without any setup.

### The MCP server reads exclusively from the cache, off a daemon prewarm

The read tools touch no network: everything an MCP client can do is read
files or sqlite rows under the cache. On startup the server kicks off
embedding-model prewarming in a **daemon thread** (so registration isn't
blocked by the ~10 s weight load) and caps native-math threads
(`OMP_NUM_THREADS=1` etc.) so concurrent MCP sessions don't oversubscribe
cores. For a remote embedding backend the prewarm is a no-op beyond
constructing the client — there are no weights to load, and it makes no
upstream call, so readiness never waits on the network. `read_file_section`
is hard-capped (default 400 lines, max 5000) as context hygiene.

The default transport is the custom threaded-writer **stdio** path (see
`_stdio_transport.py`, which sidesteps an upstream loop-blocking write).
Setting `IETF_LLM_MCP_TRANSPORT=http` serves standard MCP **Streamable
HTTP** instead — FastMCP's `streamable_http_app()` under uvicorn, with a
`GET /health` readiness route added — for a shared deployment serving
many clients from one process. Concurrency is safe because every tool
opens its own read-only sqlite connection per call (`_connect_ro`,
never shared across `anyio` worker threads) and the index is queried
read-only (no migrations on the serve path).

For a hosted deployment, `IETF_LLM_LOG_FORMAT=json` switches `log()` to
structured one-line JSON records on stderr (no secrets), for a log
collector; `IETF_LLM_DEBUG_LOG` retains the per-request timing telemetry.

The HTTP serve path runs **boot-time config validation** before binding
(`_validate_serve_config`), so a contradictory or under-provisioned
config fails fast at boot rather than minutes into a gather or on the
first `search_corpus` (upfront validation over wait-then-fail). It is
*not* transport-gated: HTTP + in-session gather is a supported
trusted-box shape (#41). It **hard-refuses** (logs the reason, exits 1)
only configs that cannot work — `ENABLE_GATHER` + `INDEX_IMMUTABLE`
(gather must write the index the mount marks read-only); gather with a
local torch-backed embed model on a torch-free image (the embed step
would crash mid-pipeline); a remote `openai-embed/...` model with no
`IETF_LLM_EMBED_BASE_URL` (the read path would fail at request time). It
**warns but never blocks** when the bind host is non-loopback (no auth or
rate limiting; assumes a trust boundary in front — the operator's call).
And it **always** logs a one-line posture banner (transport, bind, gather
on/off, embed backend, index dir, immutable) so the logs answer "what is
this process actually doing" without guessing.

### The one writer exception: opt-in in-session gather

`IETF_LLM_ENABLE_GATHER=1` registers two extra tools — `start_gather` and
`gather_status` — so a client can gather a new corpus without leaving the
session. This deliberately breaks the read-only / no-network contract, so
it is **off by default**: the shared HTTP replica stays read-only, and the
torch-free serve image is unaffected (a gather there would need a remote
`openai-embed/...` model to avoid pulling torch). `start_gather` runs the
same `__main__` pipeline as the CLI in a **daemon thread** (`gather_runner`)
and returns at once; it is not bounded by the per-call tool deadline.
Progress is recorded to a per-corpus `gather-status.json` (atomic writes)
that `gather_status` reads back — stage-level, driven by `gather_stages`
(`stage_plan` is the single source of stage order, shared with the
pipeline). One gather per corpus at a time is enforced by a non-blocking
per-corpus `file_lock`, which also serialises against a concurrent CLI
gather; different corpora gather in parallel. `gather_runner` and the
gather pipeline are imported lazily so the default serve path never pulls
them in.

### IMAP cache lives outside the per-WG directory

`imap-cache/<wg>/<list>/` rather than under `<wg>/`: the raw `.eml`
store is expensive to refetch and shouldn't be lost when a WG's
exported `files/` are cleared. Thread reconstruction walks it directly.

### The RFC series is a cross-corpus singleton, mirrored not gathered

`rfc_search` / `get_rfc` answer "find/identify/status of an RFC" across
the *whole* published series — a question no single gathered corpus can
serve. Rather than fold RFC metadata into every corpus, the series lives
once at `_rfc/` (`rfcs.json` / `refs.json` / `tags.json`), mirrored from
rfc.fyi's already-canonical, edge-cached JSON. `gather/rfcs.py` is the
only writer: each gather run refreshes it after the per-corpus work,
TTL-guarded (skip if young) and ETag-revalidated, and *never raises* —
an RFC-index hiccup must not fail a corpus gather. `rfcs.py` is the
read side, a clean-room port of rfc.fyi's search/reference semantics
(prefix-word matching, exact-number short-circuit, obsoletes-aware
inbound reference counting), rendering markdown like every other tool.
It reads only the cache and touches no network — same boundary as the
rest of the MCP server. The index is not embedded; it is a metadata
catalogue, queried directly, not via the vector store.

### The effort catalog is the matching singleton for "topic, no corpus"

The whole tool surface is corpus-first: every tool needs a corpus name,
so a topic with no obvious home ("what is the IETF doing around AI?")
had no entry point. `find_efforts(query)` closes that gap, and it leans
on the same singleton-mirror pattern as the RFC series. `gather/catalog.py`
mirrors the active (and BoF) slice of the Datatracker group collection
into `_catalog/`, refreshed once per gather run in the same tail
housekeeping as `_rfc/` and sharing its plumbing (`gather/_mirror.py`:
TTL guard, `If-None-Match` revalidation, never-raises). Unlike the RFC
mirror the reader-facing blob is *derived* — the raw group slices are
kept for revalidation, then projected to the slim `catalog.json` record
list (acronym, name, type, state, area, description). `catalog.py` is
the read side: it ranks efforts by a free-text topic (acronym over name
over charter description) and tags each with whether it is already
gathered here, so the model prefers a cached corpus over a fresh gather.
Read-only, no network, markdown out — same boundary as every other tool.
v1 covers active groups only; concluded efforts surface through
`rfc_search`, already-cached ones through `list_corpora`.

### Persisted config: two files per WG, plus one global

`gather.json` and `export.json` per WG (disjoint flag sets; one file
would make `--clear-config` either too broad or too narrow), plus a
single global `config.json` for tool/deployment-wide settings (embedding,
summariser) that aren't properties of any one corpus. The global scope
resolves `env > CLI > global > default`, so a container's injected
environment is authoritative; the per-WG scope is content only and holds
no secrets.

### Other normalisation invariants

- **Subject normalisation** strips `Re:`/`Fwd:`/`[list]` iteratively
  until stable (real traffic nests them).
- **Issue state** is always compared via `_state_is_open()` —
  `archive.json` ships both `open`/`closed` and `OPEN`/`CLOSED`.
- **Dates** are normalised to tz-aware on parse (`_parse_date` assumes
  UTC if naive) so mixed-tz `Date:` headers sort without TypeError.
- **`--draft` / `--mailing-list`** values are validated against
  Datatracker / mailarchive *before* `config.merge` persists them, so a
  typo doesn't stick in `gather.json`.

## Testing strategy

`tests/conftest.py` provides the `isolated_home` fixture (monkeypatches
`$HOME` to a tmp dir so tests never touch the real `~/.cache` /
`~/.config`) and an autouse `_no_datatracker` fixture that stubs every
network seam (`datatracker`, `datatracker_history`,
`datatracker_github`, `ballots`, `github_users`). Helpers synthesise
`.eml` files, GitHub archives, and
cache files.

Coverage spans: config; the digest builders and `query` filters;
people/identity consolidation and affiliations; embeddings chunking and
faceted search (with a stub model); the MCP tools (`overview`,
`read_topic`, `find_replies`, `tally_positions`, `find_citations`,
search facets, `read_file_section` caps); positions/poll heuristics;
ballots; citations; meeting clustering; synthetic-WG routing; the
`--list` and `--completion` CLIs; export mirroring; freshness; PDF
extraction; transcript context; skill install; the RFC-series
search/reference port (`test_rfcs.py`); and the in-session gather runner
and status reporting (`test_gather_runner.py`).

What's *not* unit-tested: live network paths and anything needing a
real embedding model loaded — those get manual smoke tests against real
corpora.

Run via `make test`. CI runs `make lint && make typecheck && make test`
on every push/PR across Python 3.10–3.14.

## Where to make changes

- **New gather source** → add a module under `gather/`, hook into
  `__main__.py`'s pipeline before the digest step. Skip it for
  synthetic (`x-`) WGs if it's Datatracker-backed.
- **New digest** → add a builder under `digest/`, call it from
  `generate_digests()`, export from `__init__`.
- **New MCP tool** → add a pure `tool_*` function in `mcp_server.py`,
  then a thin `@server.tool()` wrapper in `main()`. Document the
  routing in `data/skill/SKILL.md`. A tool that writes or reaches the
  network (like `start_gather`) must be registered behind an opt-in env
  gate, run its work off-thread, and be imported lazily so the read-only
  serve path stays clean.
- **New chunker** → add to `embeddings/chunking.py`, dispatch in
  `_chunk_file()`.
- **New persisted flag** → add it to the right scope's `scalars` or
  `lists` tuple in the `config.merge()` call.
- **New cache path** → add a helper in `paths.py`; never hardcode the
  layout elsewhere.
