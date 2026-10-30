# Architecture

The shape of the system for operators and contributors: what runs, where state lives, who writes
it, the rules that govern the network and the cache, and where to make changes. Usage is in the
[README](../README.md); the other [docs](README.md) own deployment, storage, models and the seed
store, and are linked where they apply.

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Overview](#overview)
- [Commands](#commands)
- [Corpus names and kinds](#corpus-names-and-kinds)
- [Cache layout](#cache-layout)
  - [Who writes what](#who-writes-what)
- [Config layout](#config-layout)
- [Data flow](#data-flow)
- [Operating rules](#operating-rules)
  - [The storage seam: CorpusStore (local default, cloud-pluggable)](#the-storage-seam-corpusstore-local-default-cloud-pluggable)
  - [Use the Datatracker API; do not scrape HTML](#use-the-datatracker-api-do-not-scrape-html)
  - [Network load](#network-load)
  - [Writers are write-if-changed and atomic](#writers-are-write-if-changed-and-atomic)
  - [Concurrency: gathers and servers can overlap](#concurrency-gathers-and-servers-can-overlap)
  - [The default embedding model is local; the backend is pluggable](#the-default-embedding-model-is-local-the-backend-is-pluggable)
  - [The MCP server reads only the cache](#the-mcp-server-reads-only-the-cache)
  - [The one writer exception: in-session gather](#the-one-writer-exception-in-session-gather)
  - [The networked read exception: live Datatracker lookups and get_rfc_info](#the-networked-read-exception-live-datatracker-lookups-and-get_rfc_info)
  - [IMAP cache lives outside the per-WG directory](#imap-cache-lives-outside-the-per-wg-directory)
  - [Cross-corpus singletons: the RFC series and the effort catalog](#cross-corpus-singletons-the-rfc-series-and-the-effort-catalog)
  - [The chartered-group mirror](#the-chartered-group-mirror)
- [Package map](#package-map)
- [Testing](#testing)
- [Where to make changes](#where-to-make-changes)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

## Overview

`ietf-llm` maintains a local, LLM-queryable corpus of an IETF effort's public record: a Working
Group, a mailing list, a set of drafts. One CLI gathers and indexes. Three consumers read what was
gathered: a semantic search CLI, an MCP server, and a NotebookLM exporter.

The cache is the single source of truth. Gather is its only writer; any number of readers read just
the cache and never need gather to know about them.

## Commands

| Command | Job | Reads | Writes |
|---|---|---|---|
| `ietf-llm` | gather / refresh a corpus, build digests, build the embedding index | network | cache |
| `ietf-llm-search` | semantic search over the cache | cache | stdout |
| `ietf-llm-mcp` | expose the cache to MCP clients (Claude, Codex, etc.) | cache | stdio / HTTP (MCP) |
| `ietf-llm-export` | mirror to a local directory, or push to NotebookLM Enterprise | cache | local dir / NotebookLM |

Flags are in the [reference](reference.md). `ietf-llm --list` prints the cached corpora and
`ietf-llm --completion <shell>` prints a tab-completion script.

## Corpus names and kinds

A corpus is a named bundle of sources, and a Working Group is one preset. The code calls the name
`wg`. Gather classifies it once (`gather/sequencer.py`) as one of these kinds:

- **group** — the name resolves to a Datatracker group (WG, IRTF RG, editorial WG, BoF). It gets
  the full auto-sourced pipeline: charter, group metadata, meetings, documents, transcripts, roles,
  ballots and timeline events.
- **list** — not a group, but a list archived at mailarchive.ietf.org (IETF, IRTF or RFC-Editor:
  `last-call`, `irtf-discuss`). Only that list is gathered, from the IETF IMAP mirror.
- **custom** — not a group; content comes from explicit `--draft` / `--mailing-list` / `--github`
  (the name is a label).
- **synthetic** — an `x-` prefixed name (`x-webbotauth`). Like custom, but the group lookup is
  skipped entirely.

The first match wins: `x-` synthetic, then the generative flags (`--new-drafts`, `--author`, which
make the name a label), then Datatracker group, then mailarchive list. A name that is none of these
and has no configured sources is rejected as a likely typo rather than producing an empty corpus.
There is no `--list-only` flag; the kind is inferred. A name that is both a group and a list
resolves as the group, which already discovers its own list.

Generative flags populate a custom corpus dynamically (`--new-drafts`: a rolling window of `-00`
submissions; `--author`: a person's authored drafts) and persist, so a bare re-run re-evaluates
them. Explicit sources compose onto any kind. `--add-mentioned-drafts` pulls in drafts the corpus
cites but lacks, and keeps the accumulated set.

`corpus/identity.py` derives each corpus's kind and status from on-disk artifacts, for `ietf-llm
--list` and the MCP `list_corpora` tool alike, without touching the network.

## Cache layout

Everything gather produces lands under `~/.cache/ietf-llm/`. `paths.py` is the single source of
truth for where files live; call its helpers rather than hardcoding paths. The roots are
relocatable with `IETF_LLM_CACHE_DIR`, `IETF_LLM_CONFIG_DIR` and `IETF_LLM_INDEX_DIR` (the
per-corpus `embeddings.db` files, e.g. on tmpfs); see [storage.md](storage.md).

```
~/.cache/ietf-llm/
├── <wg>/                                 # one dir per corpus
│   ├── files/                            # what consumers read
│   │   ├── charter.txt
│   │   ├── group.md                      # name, status, parent area, Additional Resources
│   │   ├── digests/                      # index issues pulls threads people timeline
│   │   │                                 # citations message_citations (.md)
│   │   ├── drafts/                       # draft-*.txt, rfc*.txt
│   │   ├── meetings/<code>/              # minutes.md agenda.md attendance.md/.json
│   │   │   ├── slides/<slug>.pdf(.txt)   #   (meetings/_orphans/ holds unmatched transcripts)
│   │   │   ├── transcripts/<YYYYMMDDHHmm>.md
│   │   │   └── polls/<YYYYMMDDHHmm>.md
│   │   ├── threads/<date>-<slug>.md      # one reconstructed mailing-list thread each
│   │   ├── issues/<repo-slug>/<N>.md     # one GitHub issue each
│   │   ├── pulls/<repo-slug>/<N>.md      # one GitHub pull request each
│   │   ├── ballots/<draft-name>.md       # IESG ballot positions per draft
│   │   ├── github/<repo-slug>.json       # raw archive (internal; not exported)
│   │   └── raw/                          # not indexed; grep / NotebookLM only
│   │       ├── mail-archive-<YYYY>.txt
│   │       └── github-<repo-slug>.txt
│   ├── embeddings.db                     # per-corpus semantic index
│   ├── topics.json                       # topic-map sidecar (theme centroids + labels)
│   ├── materials.json                    # doc-name → rev last fetched; layout stamps
│   ├── documents.json                    # draft-name → {expires, state}
│   ├── last-gathered                     # ISO-8601 freshness sentinel
│   ├── last-accessed                     # last read (local backend; stamped coarsely)
│   ├── gather-status.json                # in-session gather progress
│   └── gather-metrics.json               # last run's upstream HTTP load
├── imap-cache/<wg>/<list>/<uid>.eml      # raw fetched mail
├── transcripts-repo/                     # shallow clone of ietf-minutes-data
├── _rfc/                                 # RFC-series index (singleton)
├── _catalog/                             # active-effort catalog (singleton)
├── _groups/                              # chartered-group mirror (singleton, laid out like a corpus)
├── _seed/index.json                      # mirror of the seed store's catalog
├── _debug/                               # IETF_LLM_DEBUG_LOG telemetry
├── .http-cache/_shared/                  # shared ETag store for conditional GETs
├── _github-users.json                    # shared identity caches (also
├── _datatracker-people.json              #   _datatracker-github.json)
└── .live-cache.json                      # live-lookup cache (see the networked read exception)
```

The RFC full-text corpus `rfcs` is an `embeddings.db` under the index dir with no `files/`; it is
installed from the seed store, not gathered (see [seed-store.md](seed-store.md)). Names starting
with `_` are machinery and are excluded from `list_corpora` and `ietf-llm --list`.

### Who writes what

- **Gather writes everything.** Readers write only best-effort bookkeeping: the `last-accessed`
  stamp, `.live-cache.json`, `_seed/index.json` (refreshed by `list_corpora`), `_debug/` telemetry
  when `IETF_LLM_DEBUG_LOG` is set, and an in-place index schema upgrade (all described below).
  Anything outside `<wg>/files/` is intermediate state owned by gather.
- **Mailing lists** are kept as `threads/`, one file per reconstructed thread (from `In-Reply-To` /
  `References`, quoted runs collapsed, headers in UTC), which is what the tools read, and as
  `raw/mail-archive-<YYYY>.txt`, a flat year dump kept for NotebookLM. The dump is not indexed, and
  `grep_corpus` skips it unless asked. A thread's date is its first message.
- **GitHub issues and PRs** are sibling trees built from the repo's published `archive.json` (REST
  API as fallback). `pulls/` is written but **not embedded**, so `search_corpus`, `read_topic` and
  `find_related` do not see PR text; reach it through `get_issue`, `read_digest(kind="pulls")`,
  `grep_corpus` and `get_by_url`. On the REST fallback, an issue whose comment paging fails keeps its
  previous cached copy instead of being written truncated.
- **`digests/*.md` are deterministic** and regenerated every gather; read them through
  `read_digest` / `overview`.
- **`ballots/<draft>.md`** holds the current IESG ballot for drafts with ballot activity in the
  `--months` window. If a ballot fetch fails, the existing files are left as they were; only a
  successful "no activity" answer removes them.
- **People** are consolidated by `people/` into one record per actor across email variants and
  GitHub logins, with lookups cached across corpora in `_datatracker-people.json` and
  `_datatracker-github.json`. Meeting attendance and transcript speakers link to existing people
  and never create one.
- **`embeddings.db`** records the embedding model id, chunker version and vector dimension in
  `meta`. A model or dimension change rebuilds the index; a chunker change re-chunks but reuses
  stored vectors, and a changed file re-embeds only its changed chunks. Not embedded: `raw/`,
  `github/`, `pulls/`, digests, PDFs, and the revision stack of a draft whose state is `rfc` or
  `repl` (those files stay on disk for reading and citing).
- **`topics.json`** is written beside the index by gather's topic-map stage and recomputed only
  when the index changed. A cache gathered before it existed has none until re-gathered; `overview`
  degrades without it. `which_corpus` routing reads the sidecars of every corpus and compares only
  within one embedding model id.
- **`last-gathered`** is written at the end of each gather. With the persisted `months` window it
  tells a client how far back a corpus reaches (`coverage.py`). The window bounds mail and meetings
  only; GitHub issues and drafts are not windowed.
- **`gather-metrics.json`** records the last run's upstream HTTP requests, bytes and per-host
  breakdown. It is neither indexed nor exported.

## Config layout

```
~/.config/ietf-llm/
├── config.json              # global service settings
├── client_secrets.json      # GCP OAuth (NotebookLM only)
├── token.json               # cached OAuth token
└── <wg>/
    ├── gather.json          # persisted ietf-llm flags
    └── export.json          # persisted ietf-llm-export flags
```

Per-corpus flags persist in `gather.json` and `export.json`, so a bare `ietf-llm <wg>` re-runs
with the same sources; `--clear-config` wipes the corpus's directory. `--draft` and
`--mailing-list` values are validated before they are persisted, so a typo does not stick.
`config.json` holds tool-wide settings that are not properties of a corpus (embedding model,
embed on/off, summariser) and no secrets. It resolves `env > CLI > global > default`; see
[models.md](models.md#global-settings). On the cloud backend per-corpus config lives in the
control plane instead of `IETF_LLM_CONFIG_DIR` ([storage.md](storage.md)); global config always
stays on the filesystem and environment.

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

Re-running gather is idempotent: it fetches what is missing, re-fetches what changed, regenerates
digests and updates the index incrementally. There is no `--update` flag. `ietf-llm-export` always
writes a complete export; for NotebookLM, create a new notebook on each update.

What a change needs from operators depends on which side it touches. Changes to how tools render
are reader-side and take effect at once. Changes to what gather writes (thread files, digests,
`group.md`, `documents.json`) are write-side: existing caches keep the old content until
re-gathered, and on the cloud backend until the re-gather is published.

## Operating rules

### The storage seam: CorpusStore (local default, cloud-pluggable)

Consumers reach the cache through `CorpusStore` (`ietf_llm/store/`), chosen by
`IETF_LLM_STORE_BACKEND` (`local` by default). Readers ask it for a local directory holding a
corpus's current version; gather hands it a finished tree to `publish`.

- **local** — the live `~/.cache/ietf-llm/<corpus>` tree is the only version. Publish is a no-op
  and the gather lease is always granted, so nothing stops two gathers of one corpus in separate
  processes from overlapping.
- **cloud** — versions are immutable and published into one S3-compatible bucket by a
  compare-and-swap pointer flip, under a per-corpus gather lease. Publish, retention, lease loss,
  accelerator caches and the bucket layout are in [storage.md](storage.md).

A version is the whole corpus root: `files/`, the index, `documents.json`, `materials.json` and the
`last-gathered` / `seed-source` sentinels are published together. Read them with
`local_corpus_dir`; a path composed from `get_cache_dir()` finds nothing on the cloud backend.
Per-corpus config uses the sibling `ConfigStore` (`config/store.py`). Seeding and seed installs are
all-or-nothing: a failure leaves the cache as it was.

Both gather drivers (the `ietf-llm` CLI and the MCP runner) go through `gather/lifecycle.py`: lease
and fleet slot, seed, cache hydration, gather, publish, cache persistence, status. A CLI gather on
the cloud backend therefore publishes exactly as an in-session one does.

### Use the Datatracker API; do not scrape HTML

**When the data is available from the Datatracker REST API, gather uses it rather than parsing a
rendered HTML page.** The API ([https://datatracker.ietf.org/api/](https://datatracker.ietf.org/api/),
browsable under `/api/v1/`) returns structured fields and canonical IDs. It is the source for:

- **Group metadata and resources** — `group/group`, `group/groupextresource`.
- **Charter** — revision from the document API, text from the published
  `www.ietf.org/charter/<doc>-<rev>.txt`. While a group is rechartering, `charter.txt` holds the
  approved base revision and a header line names the proposal.
- **Documents** — drafts, RFCs and session polls via `doc/document`, paginated.
- **Meetings and materials** — sessions via `meeting/session`, resolved in batches. Material
  content is fetched from `/meeting/<n>/materials/<docname>` with `Accept: text/markdown`. A
  non-text agenda or minutes (a PDF) becomes a one-line pointer. Slide PDFs are capped at 50 MB,
  120 s and 300 pages.
- **Roles, ballots and governance events** — `group/role`, `iesg` and document-event endpoints.
  Ballot fetches report "unreachable" rather than "none" on failure, so an outage cannot clear
  gathered ballot files.

`BeautifulSoup` remains only for the few older minutes authored in HTML
(`gather/sources/meetings.py`). New gather code that reaches for an HTML page should first confirm
the API cannot answer.

### Network load

Gather is built to be a light client of its upstreams:

- **Conditional GET.** Datatracker responses are stored with their ETag (`.http-cache/`) and
  revalidated with `If-None-Match`; a 304 reuses the stored body. The store is evicted by age
  (90 days), entry count (10,000) and size (256 MB). Two local processes sharing the directory may
  lose each other's new entries, which costs a refetch.
- **Batching.** Meetings are resolved in one `id__in` request; document listings follow
  `meta.next`.
- **Revision-gated content.** Material content cannot be fetched conditionally, so `materials.json`
  records the revision last fetched and minutes and agendas are refetched only when it changes.
  Slides and polls are fetched once.
- **`Retry-After`.** The shared session retries 429 and 5xx, but a `Retry-After` over 60 s is not
  waited out: the response is returned and the stage's own error handling runs. The remote
  embedding client does the same (30 s for a query-time embed) and, on a rate limit, stops
  submitting files and keeps those already embedded; the rest retry on the next gather.
- **Per-host governor.** `net/http_governor.py` caps concurrent requests per host (datatracker
  tightest); see [mcp-server.md](mcp-server.md).

### Writers are write-if-changed and atomic

Writers that regenerate content every gather (threads, issues, ballots, digests, minutes) write a
file only if its bytes changed, so a byte-identical re-render leaves the mtime alone. Every-gather
corpus writes go through a temp file and `os.replace` (`atomicio.py`), so a reader sees the old
bytes or the new.

The embedder skips unchanged content by hash, stable across hosts, so a cloud replica that
materialises a published version does not re-embed it. The index build begins by pruning chunks for
files that are no longer eligible (a removed thread, or a draft that has become `rfc`), so a change
to the eligibility rules takes effect on the next gather without `--rebuild-embeddings`.

Temp files are dot-prefixed `.tmp` names, skipped by file listings, `grep_corpus`, the cloud publish
and the seed bundle. A killed writer leaves its temp behind; each gather sweeps temps older than an
hour from the corpus directory, its mail cache and the shared directories. Read tools never delete
anything.

### Concurrency: gathers and servers can overlap

An MCP server may answer queries while a gather rewrites the same corpus, and several gathers or
servers can run at once.

- **Readers** open a read-only SQLite connection per tool call (`mode=ro`), so a read cannot create
  an index and never blocks a writer. An absent index reads as "no index"; a present but unreadable
  one is reported as unreadable (`search_corpora` skips it and answers from the rest).
- **Index builds** write a scratch database seeded from the live index and `os.replace` it into
  place, so a query sees the previous index until the swap and never a partial one. Concurrent
  builds of one corpus serialise on a per-corpus file lock. Connections use WAL with a 30 s busy
  timeout. A read-only mount needs `IETF_LLM_INDEX_IMMUTABLE=1` ([storage.md](storage.md)).
- **Corpus files** are written atomically (above). The transcripts clone is guarded by a file
  lock; JSON side-caches (`.http-cache` index, `materials.json`, identity caches) are
  last-writer-wins, which at worst costs a refetch.
- **Gathers of one corpus** are not excluded across processes on the local backend. Across hosts
  (cloud backend) the per-corpus gather lease and the publish pointer compare-and-swap do that job,
  and a fleet gather-slot semaphore caps concurrent gathers ([storage.md](storage.md)).

### The default embedding model is local; the backend is pluggable

`sentence-transformers/BAAI/bge-small-en-v1.5` is the default: about 130 MB, no API key, on CPU,
installed with the optional `local-embeddings` extra (torch is not in the base install). The model
id selects the backend by prefix: `sentence-transformers/` loads the local model;
`openai-embed/<model>` calls an OpenAI-compatible `/v1/embeddings` endpoint configured from the
environment and needs no torch; anything else goes through `llm`. Variables and setup are in
[models.md](models.md).

- **The index records its model id**, and search reads it back to resolve the same backend.
  Vectors are not portable across backends, so switching backend re-embeds the corpus on the next
  gather, and cross-corpus search groups results by model id.
- **Queries to bge models carry the bge query instruction**, prepended at search time only, so no
  stored vector changes. `IETF_LLM_QUERY_PREFIX=off` disables it. `which_corpus` routing does not
  use it.
- **A read may upgrade an index's schema in place**: it adds columns to a local index, fetches
  nothing, and declines when the migration is expensive or the index is immutable. On any failure
  the tool tells the user to gather.
- **`--summarize` needs explicit setup**; embedding does not ([models.md](models.md)). Digests
  gathered earlier keep their old summary column until re-gathered with `--summarize`.

#### Embedding device

The local backend runs on CPU and auto-selects CUDA when present; it avoids MPS (the Apple Silicon
GPU) because of memory growth. Vectors are numerically equivalent across devices, so changing the
device needs no re-embed. `IETF_LLM_EMBED_DEVICE` (`cpu` / `mps` / `cuda`) overrides; see
[models.md](models.md).

### The MCP server reads only the cache

Read tools touch no network: they read files or SQLite rows under the cache. The routing between
tools is in `ietf_llm/data/mcp-instructions.md` (served to clients as the MCP `instructions`
field) and each tool's docstring; this document does not restate them. At startup the server
prewarms the embedding model in a daemon thread, silently (set `IETF_LLM_DEBUG_LOG` to see a
failure), and caps native-math threads so concurrent sessions do not oversubscribe cores. A remote
embedding backend has nothing to prewarm.

- **Transport.** stdio by default; `IETF_LLM_MCP_TRANSPORT=http` serves Streamable HTTP with
  `GET /health` and `GET /metrics`. Deployment, boot-time validation, logging and metrics are in
  [mcp-server.md](mcp-server.md); stdio setup is in [mcp-local.md](mcp-local.md).
- **Deadlines and scans.** Each tool call runs in a worker thread with a deadline
  (`IETF_LLM_TOOL_TIMEOUT`, default 120 s). `grep_corpus` draws from its own worker pool, and its
  regex scans run in a child process killed at a time budget; an over-budget scan reports
  "INCOMPLETE SCAN", never zero matches. Budgets and memory are under
  [Limits](mcp-server.md#limits) and [Sizing the container](mcp-server.md#sizing-the-container).
- **Names are validated** before they become paths, so a mistyped corpus is a miss and does not
  create a cache directory.

### The one writer exception: in-session gather

`start_gather` / `gather_status` / `stop_gather` let a client gather without leaving the session.
They break the read-only, no-network rule, so the default follows the transport: **on** for a local
stdio server, **off** for the shared HTTP replica. `IETF_LLM_ENABLE_GATHER` overrides either way
(`freshness.py`); when off, the tools are not registered. The setting also drives the "go gather"
hints in tool output.

- **Same pipeline as the CLI**, not bounded by the tool deadline. A daemon worker thread
  (`gather/runner.py`) owns the queue, leases, heartbeat and status; the pipeline itself runs in a
  **child process** (`gather/pipeline.py`, `gather/child.py`), so a CPU-bound index build cannot
  starve the server's event loop. `stop_gather` terminates the child, and a child crash fails one
  gather rather than the server.
- **One gather per corpus** at a time within a server process; on the cloud backend the lease
  extends that across hosts. Different corpora run in parallel. A CLI gather and an in-session
  gather in separate processes on the local backend do not exclude each other.
- **Status** is written to `<corpus>/gather-status.json` (and the control plane on cloud) and read
  back by `gather_status`. An unhandled child exception is recorded in its `error` field.
- **Reads during a gather.** A first gather refuses reads, since there is no prior snapshot. A
  re-gather stays readable: on the cloud backend readers see the previous published version until
  publish; on the local backend files change in place, so counts can differ between calls. The
  search index is the previous one until the atomic swap on both.
- **Publish only when the gather finished.** A gather skipped by the freshness debounce publishes
  nothing.

### The networked read exception: live Datatracker lookups and get_rfc_info

A narrower break from the no-network rule, behind the same gate as in-session gather.

- **Live lookups** (`live_lookup/`): `meeting_schedule`, `draft_status`, `review_record` and
  `overview(live=True)` read the Datatracker REST API directly, because schedules, draft states
  and review activity change daily. Results are cached for `IETF_LLM_LIVE_TTL` seconds (default
  300) in memory and in a best-effort `.live-cache.json`, the only thing this path writes, and
  carry their fetch time. The module is imported lazily, so the default read path loads neither it
  nor `requests`.
- **An outage is never reported as absence.** A 404 means "no such document"; any other failure
  falls back to a stale cache entry or reports "could not reach Datatracker". Fetches use short
  timeouts and no retries, so a `Retry-After` cannot hold a tool call for minutes.
- **`get_rfc_info`** answers from the `_rfc/` mirror. A **stale miss** (the number is absent and
  the mirror is past its TTL) triggers one live revalidation of `rfcs.json` (throttled and
  single-flighted); hits and in-TTL misses stay offline. When the gate is off or the fetch fails,
  the answer says the mirror is stale instead of a bare "not found". Only existence is refreshed;
  the reference graph can lag a brand-new RFC.

The seed catalog refresh in `list_corpora` is the other networked read behind this gate.

### IMAP cache lives outside the per-WG directory

`imap-cache/<wg>/<list>/` holds the raw `.eml` files, apart from `<wg>/` so that rewriting a
corpus's `files/` does not lose mail that is expensive to refetch. Thread reconstruction reads it
directly. `--clear-cache` is the one thing that removes it; mail older than the search window
cannot be fetched again, so clearing is not reversible for it.

Each list directory records the IMAP UIDVALIDITY epoch in `.uidvalidity`. When the server reports a
different value, the sync refetches and moves the old files to `<list>/epoch-<old>/`; thread
reconstruction keeps using them, and messages present in both are collapsed by `Message-Id`.
Nothing prunes an archive. On the cloud backend the mail cache is synced through the bucket
([storage.md](storage.md#gather-accelerator-caches)).

### Cross-corpus singletons: the RFC series and the effort catalog

Questions that no single corpus can answer are served by singleton mirrors at the cache root,
refreshed by gather's tail housekeeping under a TTL (`gather/sources/_mirror.py`), so a mirror
failure cannot fail a corpus gather:

- **`_rfc/`** mirrors the published RFC series from rfc.fyi (`rfcs.json`, `refs.json`,
  `tags.json`, with ETag sidecars). It is read by `search_rfc_index` and `get_rfc_info`, holds
  metadata only (never document text), and is not embedded. RFC full text is the separate `rfcs`
  corpus installed from the seed store.
- **`_catalog/`** mirrors the active and BoF slice of the Datatracker group list for
  `find_efforts`.

The readers (`singletons/`) are offline and read the JSON directly.

### The chartered-group mirror

`_groups/` is the third singleton and is laid out like a corpus (`files/` plus an `embeddings.db`
in the index dir), so the ordinary embedding, incremental-skip and seed machinery applies.
`gather/sources/groups.py` is the only writer; `singletons/groups.py` is the offline reader, and
the charter semantic search is in `mcp/groups.py`, which degrades to keyword matching when no
embedding backend is available. `IETF_LLM_GROUPS_MIN_SCORE` sets the charter-similarity floor
([mcp-server.md](mcp-server.md#configuration-reference)).

- **Contents.** Every WG and RG in any state, with the current charter, milestones, and a timeline
  (state changes, chair changes, charter revisions and approvals, milestone history, RFC
  publications joined from `_rfc/`). It excludes draft adoption and meetings. Dates before about
  2014 reflect the Datatracker import. "Current charter" means the approved one; a group
  rechartering is marked as such. It serves `get_group_info`, `search_groups`, `list_milestones`,
  `group_timeline` and, ahead of `_catalog/`, `find_efforts`.
- **Refresh.** Feeds are fetched in bulk and incrementally; a failed page keeps the stored rows.
  Charter text is fetched per group in capped batches, at most one batch every 15 minutes and
  active groups first, so a first run without a seed fills in over several gathers. A failed list
  refresh is not retried for an hour.
- **Seeding.** `_groups` is an ordinary member of the main seed store. A machine installs the
  snapshot only when it has no mirror yet, then refreshes itself ([seed-store.md](seed-store.md)).
- **Cloud.** Not supported yet: the refresh is skipped on the cloud backend, the tools report
  "not gathered yet", and `find_efforts` falls back to the active-only `_catalog/`.
- **Boundary.** Read tools never write it. It is written by the gather tail housekeeping,
  `--init`, and the seed publisher, and is write-side: nothing is visible until a refresh has run.

## Package map

```
ietf_llm/
├── cli/            # console scripts: main (ietf-llm), search, export, list, completion, skill_install
├── gather/         # sequencer (per-corpus stages), lifecycle (lease/seed/publish/status),
│   │               # runner + pipeline + child (in-session gather), cli (parser), stages, plan
│   └── sources/    # one module per thing gathered or mirrored (charter, drafts, meetings, mbox,
│                   # github, ballots, rfcs, catalog, groups, cache_sync, …)
├── digest/         # digest builders, overview, filtered digest reads, optional summariser
├── embeddings/     # chunking, sqlite index storage, model loading, search, topic map
├── mcp/            # ietf-llm-mcp: server.py (construction, registration), common.py (shared
│                   # scaffolding), serve.py (HTTP), stdio.py, one module per tool domain
├── store/          # CorpusStore + local/cloud backends, KvStore (compare-and-swap), blobs, S3
├── config/         # per-corpus config (fs and cloud ConfigStore) and service/deployment settings
├── corpus/         # corpus identity (kind/status), canonicalisation, which_corpus routing
├── seed/           # seed store: format, fetch (consumer), publish (producer), catalog
├── rfcindex/       # publisher-side reader of rfc.fyi's index; never imported by mcp/
├── singletons/     # offline readers for _rfc, _catalog and _groups
├── live_lookup/    # live Datatracker reads (the networked read path)
├── people/         # identity registry, affiliations, position extraction
├── net/            # gather-side HTTP: pooled session, per-host governor, egress metrics
├── paths.py        # filesystem layout; the one place paths are defined
├── atomicio.py     # atomic writes, file locks, directory swaps, stale-temp sweep
├── access_stamp.py # coarse read-access stamp behind --used-within
├── freshness.py    # last-gathered sentinels, staleness, the gather gate
├── regexscan.py    # bounded regex scan child for grep_corpus
├── export.py, notebooklm.py   # mirror / NotebookLM export
├── serve_metrics.py           # Prometheus registry for GET /metrics
└── data/           # mcp-instructions.md (routing brain), vendored IETF skills
```

## Testing

`make test lint typecheck` is the gate (see `CLAUDE.md`); CI runs it across Python 3.10–3.14.
`tests/conftest.py` provides `isolated_home` (points `$HOME` at a temp directory so tests never
touch the real cache or config) and an autouse `_no_datatracker` fixture that stubs the gather
network entry points. Live network paths and real embedding models get manual smoke tests against
real corpora.

## Where to make changes

- **New gather source** → a module under `gather/sources/`, hooked into the pipeline in
  `gather/sequencer.py` (`_gather_one`) before the digest step. Skip it for synthetic (`x-`)
  corpora if it is Datatracker-backed.
- **New digest** → a builder under `digest/`, called from `generate_digests()`.
- **New MCP tool** → a pure `tool_*` function and a thin `@server.tool()` wrapper in the matching
  `ietf_llm/mcp/<domain>.py`, registered in that module's `register()`. Document the routing in
  `data/mcp-instructions.md`. A tool that writes or reaches the network must sit behind the gather
  gate (a `register_live()` hook that `main()` calls only when the gate is open), run its work
  off-thread, and be imported lazily so the read-only serve path stays clean.
- **New chunker** → `embeddings/chunking.py`, dispatched in `_chunk_file()`.
- **New persisted flag** → add it to the right scope's `scalars` or `lists` tuple in the
  `config.merge()` call.
- **New cache path** → a helper in `paths.py`; never hardcode the layout elsewhere.
