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

## Cache layout

Everything the gather produces lands under `~/.cache/ietf-llm/`:

```
~/.cache/ietf-llm/
├── <wg>/                                   # one directory per WG
│   ├── files/                              # human-readable corpus
│   │   ├── <wg>-charter.txt
│   │   ├── <wg>-mailing-list-<year>.txt
│   │   ├── <wg>-github-<owner>-<repo>.json   # internal (not exported)
│   │   ├── <wg>-github-<owner>-<repo>.txt
│   │   ├── draft-ietf-<wg>-<name>-NN.txt
│   │   ├── rfc<N>.txt
│   │   ├── ietf<N>-minutes.md
│   │   ├── ietf<N>-slides-*.pdf
│   │   ├── interim<YY><wg><N>-minutes.md
│   │   ├── ietf-<wg>-<date>-transcript.md
│   │   ├── <wg>-_index.md                  # digest: landing page
│   │   ├── <wg>-_issues.md                 # digest: GitHub issues
│   │   └── <wg>-_threads.md                # digest: mailing list threads
│   └── embeddings.db                       # semantic search index
│
├── imap-cache/<wg>/<list_name>/
│   └── <uid>.eml                           # raw fetched messages
│
└── transcripts-repo/                       # shallow git clone of upstream
```

Key invariants:

- **`<wg>/files/` is what consumers read.** Anything an MCP tool, the
  search CLI, or the exporter can see lives there. Everything else
  in the cache is intermediate state owned by the gather pipeline.
- **The mailing list is materialised in two shapes.** `<wg>-mailing-list-YYYY.txt`
  is the legacy flat year-dump (kept around for grep and NotebookLM
  upload, but excluded from the embedding index). `<wg>-thread-<date>-<slug>.md`
  is one file per reconstructed thread, built via RFC 5322
  In-Reply-To / References headers with a normalised-subject safety
  net, with quoted runs collapsed and an outline at the top. The
  thread files are the form an LLM should actually read; the year
  files are there for human/external tools.
- **Identities are consolidated up front.** `ietf_llm.people.Registry`
  scans the mail + GitHub data, fetches chairs / ADs / advisors from
  Datatracker, and merges the surface forms of one actor (DMARC-rewritten
  variants, Datatracker / mailman relay addresses, multiple email
  accounts, GitHub logins matching an email local-part) into a single
  canonical name with their formal WG roles attached. Threads, digests,
  and the `<wg>-_people.md` digest (which leads with a "Working Group
  leadership" table) all surface the canonical name, so an LLM doesn't
  have to figure out that `mnot=40mnot.net@dmarc.ietf.org`,
  `Mark Nottingham via Datatracker <noreply@ietf.org>`, and the
  GitHub login `mnot` are the same person — or that he's a chair.
- **The `_*.md` files are digests** — small, deterministic, LLM-friendly
  summaries of what's in `files/`. They're regenerated on every gather.
- **`embeddings.db` is per-WG.** Different WGs can use different
  embedding models if you want; the model id is recorded in the DB's
  `meta` table and the search code reads it back at query time. The
  `chunks` table also carries `start_line` / `end_line` (1-indexed,
  inclusive) so the agent can cite "lines 342-358 of vocab-06.txt",
  and `chunk_date` (ISO 8601 UTC, NULL for windowed draft chunks)
  for faceted-by-date search. Schema is versioned; `_open_db`
  migrates older DBs forward via ALTER TABLE.
- **`imap-cache/<wg>/<list_name>/`** is the *only* place that holds raw
  per-message `.eml` files. The threads digest walks that tree directly;
  the per-year `*-mailing-list-*.txt` files are a flattened text export
  for consumers that can't deal with eml.

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

The split into two scoped files is deliberate: the gather tool and the
export tool have non-overlapping flag sets, and a user reasoning about
"how is this WG configured" should be able to read one or the other
without seeing irrelevant settings.

`ietf-llm <wg> --clear-config` wipes the entire `<wg>/` config dir
(both scopes).

## Data flow

```
                                  ┌─────────────────────────────┐
                                  │  ~/.cache/ietf-llm/<wg>/    │
                                  │                             │
   Datatracker ──┐                │   files/                    │
   IMAP archive ─┤                │     ├── *.txt, *.md         │
   GitHub API   ─┼──> ietf-llm ──>│     ├── _index.md           │
   transcripts  ─┘   (gather)     │     ├── _issues.md          │
                                  │     └── _threads.md         │
                                  │   embeddings.db             │
                                  └──────────────┬──────────────┘
                                                 │
                ┌────────────────────────────────┼──────────────────────────┐
                │                                │                          │
                ▼                                ▼                          ▼
        ietf-llm-search               ietf-llm-mcp                 ietf-llm-export
        (CLI, stdout)                 (stdio MCP server)           (local dir / NotebookLM)
```

Once gathered, the cache is consumer-agnostic. Adding a new consumer
is a matter of writing code that reads the cache; no change to gather.

## Package layout

```
ietf_llm/
├── __main__.py             # `ietf-llm` (gather) — argparse, persisted config,
│                           # orchestration: charter → meetings → mailing list →
│                           # transcripts → drafts → github → digests → embed
├── export_cli.py           # `ietf-llm-export` entry point
├── export.py               # the actual mirror + NotebookLM logic
├── search_cli.py           # `ietf-llm-search` entry point
├── mcp_server.py           # `ietf-llm-mcp` (FastMCP stdio server + pre-warm)
├── skill_install.py        # --install-claude-skill helper
├── config.py               # generic per-WG, per-scope JSON config
├── data/skill/             # bundled Claude skill (package data)
│
├── digest/                 # digest generation (split for legibility)
│   ├── __init__.py             # generate_digests() + re-exports
│   ├── helpers.py              # subject normalisation, date parsing,
│   │                           # state case-folding, size formatting
│   ├── summarizer.py           # optional LLM-backed one-liner wrapper
│   ├── issues.py               # GitHub issues digest builder
│   ├── threads.py              # mailing list threads digest builder
│   └── index.py                # corpus index + file categorisation
│
├── embeddings/             # semantic search (split for legibility)
│   ├── __init__.py             # public surface + re-exports
│   ├── chunking.py             # per-message / per-issue / windowed chunkers
│   ├── storage.py              # sqlite schema, vector packing, lookup
│   ├── models.py               # embedding-model loading + process-level cache
│   └── search.py               # build_index() and search()
│
├── charter.py              # fetch + clean charter from Datatracker
├── drafts.py               # fetch active drafts + RFCs
├── meetings.py             # fetch minutes/slides/agendas
├── transcripts.py          # fetch from ietf-minutes-data repo
├── mbox.py                 # IMAP fetch + per-year .txt export
├── github.py               # archive.json (gh-pages) or REST API
├── notebooklm.py           # Google OAuth + Discovery Engine API
└── utils.py                # log(), Verbosity/LogLevel enums,
                            # cache/config dir helpers, HTTP defaults
```

## Key design decisions

These are the ones worth knowing before you make changes.

### Cache is the only durable state

Every consumer reads from the cache. The cache is the contract.
Adding a consumer never requires touching the gather pipeline; gather
never has to know who reads its output. This is the project's main
architectural lever.

### `ietf-llm` (gather) has no `--update` flag

Re-running `ietf-llm <wg>` is idempotent: it fetches what's missing,
re-fetches what's changed, regenerates digests, and incrementally
updates the embedding index. There's no separate "update" mode
because there's no separate "first run" mode either.

### `ietf-llm-export` always does a full export

When the cache changes, the next export produces a complete fresh
output. No delta tracking, no sidecar state file recording "what we
sent last time." For NotebookLM the recommended workflow is to create
a new notebook each update rather than try to merge changes into an
existing one — this is the simplest contract that doesn't lie about
what was uploaded.

### The default embedding model is local

`sentence-transformers/BAAI/bge-small-en-v1.5` ships as the default.
~130 MB, MPS-accelerated on Apple Silicon, no API key. Auto-downloaded
on first `--embed` use. Means `pipx install ietf-llm` plus `ietf-llm
<wg> --embed` is a zero-config path to working semantic search.

The user can override with `--embed-model <id>` for any model `llm`
knows about; the model id is persisted in the embeddings DB so the
search side picks it up automatically.

### `--summarize` requires explicit setup; `--embed` doesn't

Embeddings tolerate a small local model fine. Summarisation doesn't:
a bad summary is worse than no summary because the digest's value is
that you can trust it at a glance. So summarisation defers to
whatever LLM the user has configured via the `llm` package; if they
haven't configured one, we emit a multi-line setup help instead of
limping along with a too-small model. Deterministic digests still
ship without setup.

### The MCP server pre-warms the embedding model at startup

Without pre-warming, the first `search_corpus` MCP tool call takes
~10s for the lazy weight-load, which looks like a hang to the user.
The server inspects the cache at startup, finds the model id from
the first WG's embeddings DB, and triggers a dummy embed before
entering the protocol loop. Failure is non-fatal (lazy load still
works as a fallback).

### MCP server reads exclusively from the cache

The server has no network paths. Everything an MCP client can do
amounts to reading files or sqlite rows from `~/.cache/ietf-llm/`.
This means the MCP server is safe to run anywhere; the gather side
is the only part that needs network access and outbound credentials.

### MCP `read_file_section` is hard-capped at 2000 lines per call

The MCP server enforces a maximum number of lines returned from any
`read_file_section` call. This is deliberate context hygiene: an
LLM client shouldn't be able to accidentally slurp a 20 MB mbox into
its context window with one tool call. Search returns chunks (≤8000
chars each); `get_chunk_text` returns one chunk in full; raw file
reads are bounded.

### IMAP cache lives outside the per-WG directory

`~/.cache/ietf-llm/imap-cache/<wg>/<list_name>/` rather than under
`<wg>/`. Reason: the IMAP cache is shared across runs in a way the
per-WG `files/` directory isn't — clearing one WG's exported files
shouldn't blow away thousands of fetched messages. The threads
digest walks two levels deep here (per-list subdirectories).

### Persisted config is two files per WG, not one

`gather.json` and `export.json`. The two CLIs have disjoint flag
sets, and persisting them in one file would make `--clear-config`
either too broad ("nuke everything") or too narrow ("clear which
key?"). Two files keeps the model clean.

### Subject normalisation collapses Re:/Fwd:/[list] iteratively

The regex strips one prefix at a time until the subject is stable.
Real-world list traffic includes things like `Re: [wg] Re: Fwd:
[wg] Subject` and a one-shot regex misses the inner prefixes.

### Issue state comparisons are case-insensitive

GitHub's `archive.json` ships state values in two forms: `open` /
`closed` (REST API convention) and `OPEN` / `CLOSED` (GraphQL
convention). The digest passes every state value through
`_state_is_open()` which case-folds. Don't compare states directly
to string literals anywhere.

### Dates are normalised to tz-aware on parse

IETF mailing list traffic includes both tz-aware and tz-naive
`Date:` headers; comparing them at sort time raises TypeError.
`_parse_date()` always returns aware (assumes UTC if naive) so the
rest of the code can compare freely.

## Testing strategy

```
tests/
├── conftest.py             # `isolated_home` fixture: monkeypatches $HOME
│                           # to a tmp dir so tests never touch the real
│                           # ~/.cache or ~/.config. Plus tiny helpers for
│                           # synthesising eml files, GitHub archives, etc.
├── test_config.py          # per-WG scoped config: load/save/merge/clear
├── test_digest_helpers.py  # subject normalisation, date parsing,
│                           # state case-folding, addr formatting
├── test_digest_index.py    # file categorisation
├── test_digest_issues.py   # end-to-end issues digest (both case styles)
├── test_digest_threads.py  # end-to-end threads digest (IMAP path layout,
│                           # subject grouping, mixed-tz dates)
├── test_embeddings_chunking.py  # chunkers + eligibility filter
├── test_export.py          # mirror: fresh / no-op / prune / propagate
├── test_mcp_server.py      # _safe_path, line caps, list filter
└── test_skill_install.py   # fresh / idempotent / refuse-edit / --force
```

What's tested: every pure helper, every file-IO path, every
case-folding / sort / dispatch decision.

What's *not* tested: network paths (`charter`, `drafts`, `meetings`,
`transcripts`, `mbox`'s IMAP side, `github`'s REST side,
`notebooklm`), and anything that needs a real embedding model loaded
(`embeddings.search` / `build_index` / `_get_embed_model`). Those
get manual smoke tests against real corpora.

Tests run via `make test` (which invokes pytest in the project venv).
CI runs `make lint && make typecheck && make test` on every push and
PR across Python 3.10–3.14.

## Where to make changes

- **New gather source** → add a module like `meetings.py`, hook into
  `__main__.py`'s pipeline before the digest step.
- **New digest** → add a builder under `digest/`, call it from
  `digest/__init__.py:generate_digests()`, export from `__init__`.
- **New MCP tool** → add to `mcp_server.py`'s `main()`, calling a pure
  function defined alongside (so it can be tested without MCP).
- **New chunker** → add to `embeddings/chunking.py`, dispatch in
  `_chunk_file()`.
- **New persisted flag** → add it to the right scope's `scalars` or
  `lists` tuple in `__main__.py` or `export_cli.py`'s call to
  `config.merge()`. The config module handles the rest.
