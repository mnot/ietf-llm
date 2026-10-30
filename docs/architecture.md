# Architecture

The shape of the system for operators and contributors: what runs, where state lives, who writes
it, the rules that govern the network and the cache, and where to make changes. Usage is in the
[README](../README.md); the other [docs](README.md) own deployment, storage, models and the seed
store, and are linked where they apply.

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Overview](#overview)
- [The four CLIs](#the-four-clis)
- [Corpus names and kinds](#corpus-names-and-kinds)
- [Cache layout](#cache-layout)
  - [Who writes what](#who-writes-what)
- [Config layout](#config-layout)
- [Data flow](#data-flow)
- [Operating rules](#operating-rules)
  - [Cache is the only durable state](#cache-is-the-only-durable-state)
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

The cache is the single source of truth. There is one writer (gather) and any number of
independent readers; a reader never needs gather to know about it.

## The four CLIs

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
`wg` for historical reasons. Gather classifies it once (`gather.sequencer._resolve_corpus_shape`)
into one of four kinds:

- **group** — the name resolves to a Datatracker group (WG, IRTF RG, editorial WG, BoF). It gets
  the full auto-sourced pipeline: charter, group metadata, meetings, documents, transcripts, roles,
  ballots and timeline events.
- **list** — not a group, but a list archived at mailarchive.ietf.org (IETF, IRTF or RFC-Editor:
  `last-call`, `irtf-discuss`). Only that list is gathered, from the IETF IMAP mirror.
- **custom** — not a group; content comes from explicit `--draft` / `--mailing-list` / `--github`
  (the name is a label).
- **synthetic** — an `x-` prefixed name (`x-webbotauth`). Like custom, but the group lookup is
  skipped entirely (`paths.is_synthetic_wg`).

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
├── .http-cache/_shared/                  # shared ETag store for conditional GETs
├── _github-users.json                    # shared identity caches (also
├── _datatracker-people.json              #   _datatracker-github.json)
└── .live-cache.json                      # live-lookup cache (see the networked read exception)
```

The RFC full-text corpus `rfcs` is an `embeddings.db` under the index dir with no `files/`; it is
installed from the seed store, not gathered (see [seed-store.md](seed-store.md)). Names starting
with `_` are machinery and are excluded from `list_corpora` and `ietf-llm --list`.

### Who writes what

- **Gather writes everything; readers write nothing.** The only reader-side writes are the
  best-effort `last-accessed` stamp, `.live-cache.json`, and an in-place index schema upgrade (all
  described below). Anything outside `<wg>/files/` is intermediate state owned by gather.
- **Mailing lists exist in two shapes.** `threads/` holds one file per reconstructed thread (from
  `In-Reply-To` / `References`, quoted runs collapsed, headers in UTC) and is what the tools read.
  `raw/mail-archive-<YYYY>.txt` is a flat year dump kept for NotebookLM; it is not indexed, and
  `grep_corpus` skips it unless asked. A thread's date is its first message.
- **GitHub issues and PRs are sibling trees** built from the repo's published `archive.json`
  (REST API as fallback). `pulls/` is written but **not embedded**, so `search_corpus`,
  `read_topic` and `find_related` do not see PR text; reach it through `get_issue`,
  `read_digest(kind="pulls")`, `grep_corpus` and `get_by_url`. The archive is third-party content
  and is schema-normalised when downloaded and when read. On the REST fallback, an issue whose
  comment paging fails keeps its previous cached copy instead of being written truncated; a
  throttle stops the repo, and the gather logs how many issues were left stale.
- **`digests/*.md` are deterministic** and regenerated every gather; read them through
  `read_digest` / `overview`. `timeline.kinds.json` beside `timeline.md` lets
  `read_digest(event_kind=…)` select rows exactly.
- **`ballots/<draft>.md`** holds the current IESG ballot for drafts with ballot activity in the
  `--months` window. If a ballot fetch fails, the existing files are left as they were; only a
  successful "no activity" answer removes them.
- **People are consolidated** by `people.Registry`, which merges the surface forms of one actor
  (email variants, GitHub logins) into one `Person`, resolving addresses and logins through
  Datatracker's person records. Lookups are cached across corpora in
  `_datatracker-people.json` and `_datatracker-github.json` and re-asked after a TTL. Affiliation
  comes from Datatracker's `documentauthor` table, with the draft's Authors' Addresses block as the
  offline fallback. Meeting attendance and transcript speakers link to existing people and never
  create one.
- **`embeddings.db`** records the embedding model id, chunker version and vector dimension in
  `meta`. A model or dimension change rebuilds the index; a chunker change re-chunks but reuses
  stored vectors by `chunk_hash`, and a changed file re-embeds only its changed chunks. Not
  embedded: `raw/`, `github/`, `pulls/`, digests, PDFs, and the revision stack of a draft whose
  state is `rfc` or `repl` (those files stay on disk for reading and citing).
- **`topics.json`** is written beside the index by the topic-map stage of gather and is
  recomputed only when the index changed. A cache gathered before it existed has none until it is
  re-gathered; `overview` degrades without it. `which_corpus` routing reads the sidecars of every
  corpus (via `CorpusStore.routing_fleet_table`) and compares only within one embedding model id.
- **`last-gathered`** is written at the end of each gather. `coverage.py` combines it with the
  persisted `months` window to tell a client how far back a corpus reaches. The window bounds mail
  and meetings only; GitHub issues and drafts are not windowed.
- **`gather-metrics.json`** records the last run's upstream HTTP requests, bytes and per-host
  breakdown, counted at the one egress function (`net.governed_get`). It is neither indexed nor
  exported.

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
writes a complete export; for NotebookLM, create a new notebook on each update. Interim meeting
sessions on contiguous days are clustered into one meeting (`interim<YYYYMMDD>`) and identical
minutes or agendas attached to several sessions appear once.

What a change needs from operators depends on which side it touches. Changes to how tools render
are reader-side and take effect at once. Changes to what gather writes (thread files, digests,
`group.md`, `documents.json`) are write-side: existing caches keep the old content until
re-gathered, and on the cloud backend until the re-gather is published.

## Operating rules

### Cache is the only durable state

Every consumer reads the cache; gather does not know who reads its output. The cache is reached
through `CorpusStore` (`ietf_llm/store/`), so "the cache" means whatever the configured store
materialises locally. The local filesystem is the default.

### The storage seam: CorpusStore (local default, cloud-pluggable)

`get_corpus_store()` picks the backend from `IETF_LLM_STORE_BACKEND` (`local` by default). The
store answers two questions. On the read side, `local_cache_dir(corpus)` returns a local
directory holding the corpus's current version; consumers read it through `paths.py` as before. On
the write side, `publish(corpus, workspace)` makes a gathered tree the new current version. Setup
is in [storage.md](storage.md).

- **A version is the whole corpus root**, not just `files/` and the index: `documents.json`,
  `materials.json` and the `last-gathered` / `seed-source` sentinels are published with it. Read
  them with `local_corpus_dir`; composing a path from `get_cache_dir()` finds nothing on the cloud
  backend, whose versions are materialised into per-version scratch. Under a split
  `IETF_LLM_INDEX_DIR`, `paths.INDEX_FILE_NAMES` names the index files that move between the
  version root and the index dir; the cloud staging and the seed store both use it.
- **Existence and listing queries** (`has_file`, `list_files`) are answered from the version
  manifest on the cloud backend, so asking whether a draft exists does not download a corpus.
- **Freshness reads** for `/health`, `/metrics` and per-corpus sweeps (`last_gathered`,
  `seed_source`) use only what is already staged on the replica and make no control-plane call; a
  cold replica reports "not recorded". The `local_*` variants read the local workspace directly
  and ignore the configured backend; the CLIs and the seed producer and consumer use them.
- **Per-corpus config** rides a sibling interface, `ConfigStore` (`config/store.py`), selected by
  the same setting. On the cloud backend it lives in the control plane under
  `corpora/<name>/config/<scope>`.
- **Seeding and installs are all-or-nothing.** `seed_workspace` (cloud) and the seed installer
  stage index and content in temp directories and swap them in together (`atomicio.swap_dirs`), so
  a failure leaves every directory as it was. Scratch directories are dot-prefixed so a leaked one
  is never listed as a corpus.

**`LocalCorpusStore`** treats the live `~/.cache/ietf-llm/<corpus>` as the single version.
Publish is a no-op and the gather lease is always granted.

**`CloudCorpusStore`** keeps two planes in one S3-compatible bucket: an immutable blob plane for
versioned content, and a control plane built on get plus conditional put (`If-Match` /
`If-None-Match`). Bucket layout:

- `corpora/<name>/versions/<version>/` — immutable content and its manifest.
- `corpora/<name>/{pointer,lease,status,access}` — current version, gather lease, gather status,
  last read access.
- `corpora/<name>/config/<scope>` — per-corpus config.
- `fleet/slots` — the gather-slot semaphore.
- `corpora/<name>/gather-cache/` and `fleet/gather-cache/`, `fleet/catalog/` — gather accelerator
  caches (gather-only; the serve path never reads them).

Rules:

- **Publish** stages content and a manifest under a fresh version prefix, then flips the pointer
  with one compare-and-swap. Readers see the old version or the new one, never a mix, and a killed
  publish leaves the previous version live. It first checks the publisher still holds the gather
  lease and raises `LeaseLost` otherwise.
- **Retention.** After the flip, publish deletes older versions beyond `IETF_LLM_RETAIN_VERSIONS`
  (default 2), so no bucket lifecycle rule is needed. Keeping the previous version keeps a
  slightly stale replica reading valid data; a read whose version was reaped re-resolves and
  retries.
- **Reads** resolve the current version through a per-replica cache of `IETF_LLM_RESOLVE_TTL`
  seconds (default 10); a publish is visible on other replicas within that time.
- **Seed on gather.** A gather first materialises the current version into its workspace, so a
  re-gather on a fresh host builds on prior output; a seed failure degrades to a full gather.
- **Lease loss.** A gather whose lease renewal is refused, errors three times in a row, or goes
  three quarters of the TTL without succeeding stops at its next checkpoint, publishes nothing,
  and records `failed` locally without overwriting the new holder's status record.
- **Gather accelerator caches** are hydrated before a gather and persisted after, best-effort, so
  an ephemeral host revalidates upstreams instead of refetching: the datatracker ETag store
  (`corpora/<name>/gather-cache/`, one tar per corpus, rewritten under the lease), the per-list
  IMAP mail (same prefix, see below), the shared identity maps (`fleet/gather-cache/`, merged with
  compare-and-swap) and the effort catalog (`fleet/catalog/`, last writer wins). A sync failure costs a cold rebuild, never a failed gather, with one
  exception noted under the IMAP cache. `_rfc/` is not synced; it is refetched from its CDN.
- **Access recording.** Readers write one coarse `access` key per corpus for the `--used-within`
  refresh filter; `IETF_LLM_RECORD_ACCESS=off` disables it ([storage.md](storage.md)).

Both gather drivers (the `ietf-llm` CLI and the MCP runner) go through `gather/lifecycle.py`:
lease and fleet slot, seed, cache hydration, gather, publish, cache persistence, status. A CLI
gather on the cloud backend therefore publishes exactly as an in-session one does.

### Use the Datatracker API; do not scrape HTML

**When the data is available from the Datatracker REST API, gather uses it rather than parsing a
rendered HTML page.** The API ([https://datatracker.ietf.org/api/](https://datatracker.ietf.org/api/),
browsable under `/api/v1/`) returns structured fields and canonical IDs; HTML layout changes
without notice and a silent scrape failure shows up as "gather suddenly returns nothing".

The API is the source for:

- **Group metadata and resources** — `group/group`, `group/groupextresource`
  (`datatracker_api.fetch_group_object`, `get_group_resources`).
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

`BeautifulSoup` remains only for `get_by_url` (arbitrary user-supplied pages) and the few older
minutes authored in HTML. New gather code that reaches for an HTML page should first confirm the
API cannot answer.

### Network load

Gather is built to be a light client of its upstreams:

- **Conditional GET.** Datatracker responses are stored with their ETag (`.http-cache/`) and
  revalidated with `If-None-Match`; a 304 reuses the stored body. The store is evicted by age
  (90 days), entry count and size. Two local processes sharing the directory may lose each other's
  new entries, which costs a refetch.
- **Batching.** Meetings are resolved in one `id__in` request; document listings follow
  `meta.next`.
- **Revision-gated content.** Material content cannot be fetched conditionally, so `materials.json`
  records the revision last fetched and minutes and agendas are refetched only when it changes.
  Slides and polls are fetched once.
- **`Retry-After`.** The shared session retries 429 and 5xx, but a `Retry-After` over
  `transport.RETRY_AFTER_MAX` (60 s) is not waited out: the response is returned and the stage's
  own error handling runs. The remote embedding client does the same (30 s for a query-time
  embed) and, on a rate limit, stops submitting files and keeps those already embedded; the rest
  retry on the next gather.
- **Per-host governor.** `net/http_governor.py` caps concurrent requests per host (datatracker
  tightest); see [mcp-server.md](mcp-server.md).

### Writers are write-if-changed and atomic

Writers that regenerate content every gather (threads, issues, ballots, digests, minutes) write a
file only if its bytes changed (`atomicio.write_if_changed`), so a byte-identical re-render leaves
the mtime alone. Every-gather corpus writes also go through `atomicio.atomic_open` (temp file then
`os.replace`), so a reader sees the old bytes or the new, never a truncated file.

The embedder skips unchanged content by hash, stable across hosts, so a cloud replica that
materialises a published version does not re-embed it. `build_index` begins by pruning chunks for
files that are no longer eligible (a removed thread, or a draft that has become `rfc`), so a change
to the eligibility rules takes effect on the next gather without `--rebuild`.

Temp files are dot-prefixed `.tmp` names, skipped by `list_files`, `grep_corpus`, the cloud
publish walk and the seed bundle. A killed writer leaves its temp behind; each gather sweeps temps
older than an hour (`atomicio.sweep_stale_tmp`) from the corpus directory, its mail cache and the
shared directories. Read tools never delete anything.

### Concurrency: gathers and servers can overlap

An MCP server may answer queries while a gather rewrites the same corpus, and several gathers or
servers can run at once.

- **Readers never block readers**, and the server only reads: each tool call opens its own
  read-only SQLite connection (`mode=ro`), so a read cannot create an index. An absent index reads
  as "no index"; a present but unreadable one raises `IndexUnreadable` and is reported as such
  (`search_corpora` skips it and answers from the rest).
- **Index builds** write a scratch database seeded from the live index and `os.replace` it into
  place, so a query sees the previous index until the swap and never a partial one. Concurrent
  builds of one corpus serialise on a per-corpus file lock. Connections use WAL with a 30 s busy
  timeout. A read-only mount needs `IETF_LLM_INDEX_IMMUTABLE=1` ([storage.md](storage.md)).
- **Corpus files** are written atomically (above). The transcripts clone is guarded by a file
  lock; JSON side-caches (`.http-cache` index, `materials.json`, identity caches) are written
  temp-plus-rename and are last-writer-wins, which at worst costs a refetch.
- **Across hosts** (cloud backend) the per-corpus gather lease and the publish pointer
  compare-and-swap replace the file lock and shared-filesystem atomicity; the fleet gather-slot
  semaphore caps concurrent gathers (`IETF_LLM_GATHER_MAX_INFLIGHT`, see [storage.md](storage.md)).

### The default embedding model is local; the backend is pluggable

`sentence-transformers/BAAI/bge-small-en-v1.5` is the default: about 130 MB, no API key, on CPU,
installed with the optional `local-embeddings` extra (torch is not in the base install). The model
id selects the backend by prefix: `sentence-transformers/` loads the local model;
`openai-embed/<model>` calls an OpenAI-compatible `/v1/embeddings` endpoint configured from the
environment and needs no torch; anything else goes through `llm`. Variables and setup are in
[models.md](models.md).

- **The index records its model id**, and search reads it back to resolve the same backend.
  Vectors are not portable across backends (even the same model differs between runtimes), so
  switching backend re-embeds the corpus on the next gather, and cross-corpus search groups
  results by model id because scores are not comparable across them.
- **Queries to bge models carry the bge query instruction**, prepended at search time only, so
  it changes no stored vector. `IETF_LLM_QUERY_PREFIX=off` disables it. `which_corpus` routing
  does not use it; its confidence floor was calibrated without it.
- **A read may upgrade an index's schema in place** (`try_upgrade_schema`): it adds columns to a
  local index, fetches nothing, and declines when the migration is expensive or the index is
  immutable. On any failure the tool tells the user to gather.
- **`--summarize` needs explicit setup**; embedding does not. Summaries go through the `llm`
  package (or a remote chat endpoint), are capped at 300 characters, and sit in a column headed
  `LLM summary`. Digests gathered earlier keep their old column until re-gathered with
  `--summarize`.

#### Embedding device

The local backend runs on CPU by default and auto-selects CUDA when present. It avoids MPS because
PyTorch's MPS allocator fragments badly on variable-length input (one modest corpus reached about
11 GB on Apple Silicon, against 1–2 GB on CPU). Vectors are numerically equivalent across devices,
so changing the device needs no re-embed. `IETF_LLM_EMBED_DEVICE` (`cpu` / `mps` / `cuda`)
overrides. This works around upstream PyTorch MPS memory bugs (pytorch/pytorch#77753, #164299);
revisit the default if they are fixed. See also [models.md](models.md).

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
- **Deadlines.** Each tool call runs in a worker thread with a deadline
  (`IETF_LLM_TOOL_TIMEOUT`, default 120 s). `grep_corpus` draws from its own worker pool
  (`IETF_LLM_SCAN_CONCURRENCY`, default 4).
- **Regex scans** (`grep_corpus(regex=True)`) run in a child process (`ietf_llm.regexscan`) that
  is killed at its time budget, because Python's `re` cannot be interrupted in-process. An
  over-budget scan reports "INCOMPLETE SCAN", never zero matches. The child has no memory cap; see
  [mcp-server.md](mcp-server.md).
- **Names are validated** before they become paths (`paths.readable_corpus_name`,
  `paths.checked_corpus`), so a mistyped corpus is a miss and does not create a cache directory.
- **Bounded output.** `get_draft`, `get_issue` and `read_file_section` cap their replies by lines
  and characters and banner a partial read.

### The one writer exception: in-session gather

`start_gather` / `gather_status` / `stop_gather` let a client gather without leaving the session.
They break the read-only, no-network rule, so the default follows the transport: **on** for a local
stdio server, **off** for the shared HTTP replica. `IETF_LLM_ENABLE_GATHER` overrides either way
(`freshness.gather_enabled`); when off, the tools are not registered. The setting also drives the
"go gather" hints in tool output.

- **Same pipeline as the CLI**, not bounded by the tool deadline. A daemon worker thread
  (`gather.runner`) owns the queue, leases, heartbeat and status; the pipeline itself runs in a
  **child process** (`gather.pipeline`, `python -m ietf_llm.gather.child`), so a CPU-bound index
  build cannot starve the server's event loop. `stop_gather` terminates the child, and a child
  crash fails one gather rather than the server. `IETF_LLM_GATHER_INPROCESS` runs it in-thread
  (tests only).
- **One gather per corpus** at a time (a non-blocking file lock, which also serialises against a
  CLI gather); different corpora run in parallel.
- **Status** is written to `<corpus>/gather-status.json` (and the control plane on cloud) and read
  back by `gather_status`. An unhandled child exception is recorded in its `error` field.
- **Reads during a gather.** A first gather refuses reads, since there is no prior snapshot. A
  re-gather stays readable: on the cloud backend readers see the previous published version until
  publish; on the local backend files change in place, so counts can differ between calls. The
  search index is the previous one until the atomic swap on both.
- **Publish only on `GATHERED`.** The child's exit code reports its outcome; a gather skipped by
  the freshness debounce publishes nothing. The debounce reads the store's `gathered_at`.

### The networked read exception: live Datatracker lookups and get_rfc_info

A narrower break from the no-network rule, behind the same gate (`freshness.gather_enabled`): on
by default for stdio, off for the HTTP replica.

- **Live lookups** (`live_lookup/`): `meeting_schedule`, `draft_status`, `review_record` and
  `overview(live=True)` read the Datatracker REST API directly, because schedules, draft states
  and review activity change daily. Results are cached for `IETF_LLM_LIVE_TTL` seconds (default
  300) in memory and in a best-effort `.live-cache.json`, the only thing this path writes, and
  carry their fetch time. The module is imported lazily, so the default read path loads neither
  it nor `requests`.
- **An outage is never reported as absence.** A 404 means "no such document"; any other failure
  falls back to a stale cache entry or reports "could not reach Datatracker". Fetches use
  `governed_get(..., retrying=False)` with short timeouts, so a `Retry-After` cannot hold a tool
  call for minutes.
- **`get_rfc_info`** answers from the `_rfc/` mirror. A **stale miss** (the number is absent and
  the mirror is past its TTL) triggers one live revalidation of `rfcs.json`
  (`gather.sources.rfcs.revalidate_index`, composed in `mcp/rfcs.py`); hits and in-TTL misses stay
  offline. Revalidation is throttled and single-flighted. When the gate is off or the fetch
  fails, the answer says the mirror is stale instead of a bare "not found". Only existence is
  refreshed; the reference graph can lag a brand-new RFC.

The offline `draft_authors` tool needs no network and is always registered.
The seed catalog refresh in `list_corpora` is the other networked read behind this gate.

### IMAP cache lives outside the per-WG directory

`imap-cache/<wg>/<list>/` holds the raw `.eml` files, apart from `<wg>/` so that rewriting a
corpus's `files/` does not lose mail that is expensive to refetch. Thread reconstruction reads it
directly. `--clear-cache` is the one thing that removes it (and on the cloud backend deletes the
corpus's stored accelerator caches first, so hydrate cannot restore them). Mail older than the
search window cannot be fetched again, so clearing is not reversible for it.

Each list directory records the IMAP UIDVALIDITY epoch in `.uidvalidity`. When the server reports a
different value, the sync refetches and moves the old files to `<list>/epoch-<old>/`; thread
reconstruction keeps using them, and messages present in both are collapsed by `Message-Id`.
Nothing prunes an archive.

On the cloud backend the mail cache is round-tripped as gzip-tar chunks of 512 UIDs plus an
`index.json` (`gather/sources/cache_sync.py`), and it is the one place the cache sync is **not**
best-effort. If stored mail cannot be fetched or read, hydrate raises `GatherCacheRestoreError` and
the gather fails before any stage runs, because rebuilding from what remains would publish a
version missing the old threads. The next gather retries and nothing is published meanwhile. An
unrecoverable stored object (a missing chunk, a corrupt index) is a gather note instead. A
differing epoch replaces the stored copy only after this gather's sync confirmed the epoch with the
server (`.verified-uidvalidity`), and the old epoch is archived first.

### Cross-corpus singletons: the RFC series and the effort catalog

Questions that no single corpus can answer are served by singleton mirrors at the cache root,
refreshed by gather's tail housekeeping with a TTL guard, `If-None-Match` revalidation and
never-raises behaviour (`gather/sources/_mirror.py`), so a mirror failure cannot fail a corpus
gather:

- **`_rfc/`** mirrors the published RFC series from rfc.fyi (`rfcs.json`, `refs.json`,
  `tags.json`, with ETag sidecars). It is read by `search_rfc_index` and `get_rfc_info`, holds
  metadata only (never document text), and is not embedded. RFC full text is the separate `rfcs`
  corpus installed from the seed store.
- **`_catalog/`** mirrors the active and BoF slice of the Datatracker group list for
  `find_efforts`. The raw slices are kept for revalidation and projected to a slim `catalog.json`.

The readers (`singletons/`) are offline and query the JSON directly rather than through the vector
store. The one exception to "offline" is the `get_rfc_info` revalidation above, composed in the
tool layer so the reader module stays offline.

### The chartered-group mirror

`_groups/` is the third singleton and is laid out like a corpus (`files/` plus an `embeddings.db`
in the index dir), so the ordinary embedding, incremental-skip and seed machinery applies.
`gather/sources/groups.py` is the only writer; `singletons/groups.py` is the offline reader, and
the charter semantic search is in `mcp/groups.py`, so the reader degrades to keyword matching when
no embedding backend is available.

- **Contents.** Every WG and RG in any state, with the current charter, milestones, and a timeline
  (state changes, chair changes, charter revisions and approvals, milestone history, RFC
  publications joined from `_rfc/`). It excludes draft adoption and meetings. Dates before about
  2014 reflect the Datatracker import. "Current charter" means the approved one; a group
  rechartering is marked as such. It serves `get_group_info`, `search_groups`, `list_milestones`,
  `group_timeline` and, ahead of `_catalog/`, `find_efforts`.
- **Refresh.** Feeds are fetched in bulk and incrementally from per-feed watermarks in
  `files/state/watermarks.json`; a failed page keeps the stored rows. A first full refresh is
  about 130 requests and a daily one a handful. Charter text is fetched per group, capped per run
  (`CHARTER_BATCH`, active groups first), so a first run without a seed fills in over several
  invocations. Failures are throttled so a persistently failing charter cannot hold up the queue.
  The index pass runs only when a rendered file changed, a snapshot was installed, or an earlier
  pass did not complete (`files/state/index-pending`).
- **Seeding.** `_groups` is an ordinary member of the main seed store, refreshed in-process by
  `seed.publish`. A machine installs the snapshot only when it has no mirror yet, then refreshes
  itself ([seed-store.md](seed-store.md)).
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
├── freshness.py    # last-gathered sentinels, staleness, gather_enabled
├── regexscan.py    # bounded regex scan child for grep_corpus
├── export.py, notebooklm.py   # mirror / NotebookLM export
├── serve_metrics.py           # Prometheus registry for GET /metrics
└── data/           # mcp-instructions.md (routing brain), vendored IETF skills
```

## Testing

`make test lint typecheck` is the gate (see `CLAUDE.md`); CI runs it across Python 3.10–3.14.
`tests/conftest.py` provides `isolated_home` (points `$HOME` at a temp directory so tests never
touch the real cache or config) and an autouse `_no_datatracker` fixture that stubs the gather
network entry points. Live network paths and real embedding models are not unit-tested; they get manual
smoke tests against real corpora.

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
