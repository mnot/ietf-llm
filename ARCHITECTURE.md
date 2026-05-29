# Architecture

A short read-this-first for anyone looking at the codebase. Covers the
shape of the system, where state lives, what each module does, and the
decisions that aren't obvious from the code.

## Elevator pitch

`ietf-llm` maintains a local, LLM-queryable corpus of an IETF Working
Group's public record. One CLI gathers and indexes; three consumers
read what was gathered — a semantic search CLI, an MCP server, and a
NotebookLM exporter.

The cache is the single source of truth. Everything writes to it once
and reads from it forever.

## The four CLIs

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | gather / refresh a WG, build digests, build embedding index | network | cache |
| `ietf-llm-search` | semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | expose the cache to MCP clients (Claude, Codex, etc.) | cache | stdio (MCP protocol) |
| `ietf-llm-export` | mirror to local dir, or push to NotebookLM Enterprise | cache | local dir / NotebookLM |

This is the load-bearing shape of the project: **one writer to the
cache, three independent readers.** Any consumer can come and go
without touching the gather pipeline. Conversely, the gather pipeline
doesn't know or care which consumers are downstream.

`ietf-llm --list` prints the cached WGs; `ietf-llm --completion <shell>`
prints a shell tab-completion script.

## WG shortnames

`<wg>` is always a shortname (`httpbis`, `tls`, `aipref`). IRTF
Research Groups use the same convention (`cfrg`, `hrpc`). A shortname
prefixed with **`x-`** (`x-webbotauth`) is a *synthetic / pre-WG
corpus*: a collection of drafts and mailing lists with no formal WG
yet. Synthetic corpora skip every Datatracker / WG-page lookup (no
charter, leadership, auto-discovered drafts, or Datatracker timeline /
ballot events); only the explicit `--draft` / `--mailing-list` /
`--github` inputs drive content. `utils.is_synthetic_wg()` is the
single predicate; the prefix is the only signal (no side-table state).

## Cache layout

Everything the gather produces lands under `~/.cache/ietf-llm/`. The
layout is the responsibility of `paths.py` — the single source of
truth for where files live. Don't hardcode paths elsewhere; call the
helpers.

```
~/.cache/ietf-llm/
├── <wg>/                                 # one dir per WG / corpus
│   ├── files/                            # the corpus consumers read
│   │   ├── charter.txt
│   │   ├── group.md                      # status, parent area, Additional Resources
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
│   └── last-gathered                      # ISO-8601 sentinel (freshness)
│
├── imap-cache/<wg>/<list>/<uid>.eml       # raw fetched messages
├── transcripts-repo/                      # shallow clone of ietf-minutes-data
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
  local-part) into one canonical `Person`. A Person also carries
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
- **`embeddings.db` is per-WG.** The model id is recorded in the DB's
  `meta` table and read back at query time. The `chunks` table carries
  `start_line`/`end_line`, `chunk_date`, `labels`, `state`, `url`,
  `duplicate_of`, `closing_rationale` for faceted search. Schema is
  versioned; `_open_db` migrates older DBs forward via ALTER TABLE.
- **`imap-cache/<wg>/<list>/`** is the only place holding raw `.eml`
  files. Thread reconstruction walks that tree (two levels — one
  subdir per list, since a WG can follow several).
- **`last-gathered`** is an ISO-8601 sentinel `freshness.py` writes at
  the end of each gather; consumers surface staleness from it.

## Config layout

Per-WG, per-tool persistent flags live under `~/.config/ietf-llm/`:

```
~/.config/ietf-llm/
├── client_secrets.json                     # GCP OAuth (NotebookLM only)
├── token.json                              # cached OAuth token
└── <wg>/
    ├── gather.json                         # ietf-llm flags
    └── export.json                         # ietf-llm-export flags
```

The split into two scoped files is deliberate: the gather and export
tools have non-overlapping flag sets, and a user reasoning about "how
is this WG configured" should read one or the other without seeing
irrelevant settings. `ietf-llm <wg> --clear-config` wipes the whole
`<wg>/` config dir.

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
        (CLI, stdout)                 (stdio MCP server)           (local dir / NotebookLM)
```

Once gathered, the cache is consumer-agnostic. Adding a new consumer
is a matter of writing code that reads the cache; no change to gather.

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
├── skill_install.py        # --install-claude-skill helper
├── config.py               # generic per-WG, per-scope JSON config (merge/persist)
├── paths.py                # cache-layout single source of truth; meeting_label()
├── freshness.py            # last-gathered sentinel + staleness warnings
├── people.py               # actor/identity registry (roles, affiliations, domains)
├── positions.py            # heuristic position / poll / chair-statement extraction
├── notebooklm.py           # Google OAuth + Discovery Engine API
├── text.py                 # generic text helpers (subject norm, date, addr)
├── utils.py                # log(), Verbosity/LogLevel, cache/config dirs, HTTP
│                           # defaults, group metadata via API (type/title/list),
│                           # is_synthetic_wg, cached_wg_names,
│                           # write_if_changed, argcomplete helpers
├── data/skill/SKILL.md     # bundled Claude skill (also fed to MCP `instructions`)
│
├── gather/                 # content acquisition + per-source post-processing
│   ├── charter.py              # charter text artifact (rev from doc API)
│   ├── group_info.py           # group.md: status / area / Additional Resources
│   ├── drafts.py               # WG drafts + RFCs via doc API; --draft extras
│   ├── meetings.py             # minutes/agenda/slides via meeting API; clustering
│   ├── transcripts.py          # ietf-minutes-data repo; match to meeting clusters
│   ├── transcript_context.py   # prepend meeting-context header to transcripts
│   ├── mbox.py                 # IMAP fetch + per-year .txt; --mailing-list extras
│   ├── mail_threads.py         # reconstruct per-thread .md files
│   ├── github.py               # archive.json (gh-pages) or REST API
│   ├── github_users.py         # resolve logins → real name + company
│   ├── issue_files.py          # per-issue .md files
│   ├── datatracker.py          # roles + paginated document listing via JSON API
│   ├── datatracker_history.py  # governance / doc-lifecycle timeline events
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
│   ├── summarizer.py           # optional LLM-backed one-liner wrapper
│   ├── issues.py / threads.py / index.py   # per-kind digest builders
│   ├── timeline.py             # chronological event log (incl. Datatracker, ballots)
│   ├── overview.py             # one-call composed summary (+ charter excerpt,
│   │                           # citation counts, freshness line)
│   └── query.py                # filtered / paginated digest reads
│
└── embeddings/             # semantic search (split for legibility)
    ├── __init__.py             # public surface + re-exports
    ├── chunking.py             # per-message / per-issue / windowed chunkers
    ├── storage.py              # sqlite schema, vector packing, lookup
    ├── models.py               # embedding-model loading + process-level cache
    ├── snippet.py              # structure-aware snippet rendering for hits
    └── search.py               # build_index() and search()
```

## MCP tool surface

`mcp_server.py` registers each tool as a thin wrapper over a pure
`tool_*` function (so the logic is testable without MCP). Grouped by
job:

- **Orient:** `list_working_groups`, `overview`, `list_labels`,
  `list_files`.
- **Catalogue:** `read_digest(kind=…, …filters)` over
  issues/threads/people/timeline/index.
- **Search:** `search_corpus(query, …)` with `label`/`state`/`author`/
  `role`/`file_pattern`/`since`/`until`/`sort="date"`/`group_by="file"`/
  `snippet_chars` facets.
- **Narrative:** `read_topic` (full messages, chronological, across
  files), `find_replies` (reply tree of one message), `tally_positions`
  (grounded support/oppose/poll count + chair-statements section),
  `find_citations` (threads citing a draft).
- **Pivot / read:** `get_chunk_text`, `get_chunks_batch`,
  `fetch_by_url`, `read_file_section`.

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

### Writers are write-if-changed, not wipe-and-rewrite

`mail_threads`, `issue_files`, `ballots`, and the digest/minutes
writers regenerate content every gather but write a file only when its
bytes actually changed (`utils.write_if_changed`). A byte-identical
re-render leaves mtime untouched — load-bearing because the embedder
re-embeds any file whose mtime advanced. Orphans (a thread/issue that
no longer exists) are deleted in a separate cleanup pass.

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

### The default embedding model is local

`sentence-transformers/BAAI/bge-small-en-v1.5` ships as the default
(~130 MB, MPS-accelerated, no API key, auto-downloaded on first use).
Override with `--embed-model <id>`; the id is persisted in the
embeddings DB so search picks it up automatically.

### `--summarize` requires explicit setup; `--embed` doesn't

Embeddings tolerate a small local model fine; a bad summary is worse
than none. Summarisation defers to whatever LLM the user configured via
the `llm` package; without one, we print setup help rather than limp
along. Deterministic digests ship without any setup.

### The MCP server reads exclusively from the cache, off a daemon prewarm

No network paths: everything an MCP client can do is read files or
sqlite rows under `~/.cache/ietf-llm/`. On startup the server kicks off
embedding-model prewarming in a **daemon thread** (so registration
isn't blocked by the ~10 s weight load) and caps native-math threads
(`OMP_NUM_THREADS=1` etc.) so concurrent MCP sessions don't oversubscribe
cores. `read_file_section` is hard-capped (default 400 lines, max 5000)
as context hygiene — an LLM client can't slurp a multi-MB file in one
call.

### IMAP cache lives outside the per-WG directory

`imap-cache/<wg>/<list>/` rather than under `<wg>/`: the raw `.eml`
store is expensive to refetch and shouldn't be lost when a WG's
exported `files/` are cleared. Thread reconstruction walks it directly.

### Persisted config is two files per WG, not one

`gather.json` and `export.json`. Disjoint flag sets; one file would
make `--clear-config` either too broad or too narrow.

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
network seam (`datatracker`, `datatracker_history`, `ballots`,
`github_users`). Helpers synthesise `.eml` files, GitHub archives, and
cache files.

Coverage spans: config; the digest builders and `query` filters;
people/identity consolidation and affiliations; embeddings chunking and
faceted search (with a stub model); the MCP tools (`overview`,
`read_topic`, `find_replies`, `tally_positions`, `find_citations`,
search facets, `read_file_section` caps); positions/poll heuristics;
ballots; citations; meeting clustering; synthetic-WG routing; the
`--list` and `--completion` CLIs; export mirroring; freshness; PDF
extraction; transcript context; skill install.

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
  routing in `data/skill/SKILL.md`.
- **New chunker** → add to `embeddings/chunking.py`, dispatch in
  `_chunk_file()`.
- **New persisted flag** → add it to the right scope's `scalars` or
  `lists` tuple in the `config.merge()` call.
- **New cache path** → add a helper in `paths.py`; never hardcode the
  layout elsewhere.
