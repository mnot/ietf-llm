# Architecture

A short read-this-first for anyone looking at the codebase. Covers the
shape of the system, where state lives, what each module does, and the
decisions that aren't obvious from the code.

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Elevator pitch](#elevator-pitch)
- [The four CLIs](#the-four-clis)
- [Corpus names and kinds](#corpus-names-and-kinds)
- [Cache layout](#cache-layout)
- [Config layout](#config-layout)
- [Data flow](#data-flow)
- [Package layout](#package-layout)
- [MCP tool surface](#mcp-tool-surface)
- [Key design decisions](#key-design-decisions)
  - [Cache is the only durable state](#cache-is-the-only-durable-state)
  - [The storage seam: CorpusStore (local default, cloud-pluggable)](#the-storage-seam-corpusstore-local-default-cloud-pluggable)
  - [Use the Datatracker API; do not scrape HTML](#use-the-datatracker-api-do-not-scrape-html)
  - [Network is minimised three ways](#network-is-minimised-three-ways)
  - [Writers are write-if-changed and atomic](#writers-are-write-if-changed-and-atomic)
  - [Concurrency: gathers and servers can overlap](#concurrency-gathers-and-servers-can-overlap)
  - [`ietf-llm` (gather) has no `--update` flag](#ietf-llm-gather-has-no---update-flag)
  - [Interim sessions are clustered into one meeting](#interim-sessions-are-clustered-into-one-meeting)
  - [`ietf-llm-export` always does a full export](#ietf-llm-export-always-does-a-full-export)
  - [The default embedding model is local; the backend is pluggable](#the-default-embedding-model-is-local-the-backend-is-pluggable)
  - [`--summarize` requires explicit setup; embedding doesn't](#--summarize-requires-explicit-setup-embedding-doesnt)
  - [The MCP server reads exclusively from the cache, off a daemon prewarm](#the-mcp-server-reads-exclusively-from-the-cache-off-a-daemon-prewarm)
  - [The one writer exception: in-session gather](#the-one-writer-exception-in-session-gather)
  - [The networked read exception: live Datatracker lookups](#the-networked-read-exception-live-datatracker-lookups)
  - [IMAP cache lives outside the per-WG directory](#imap-cache-lives-outside-the-per-wg-directory)
  - [Cross-corpus singletons: the RFC series and the effort catalog](#cross-corpus-singletons-the-rfc-series-and-the-effort-catalog)
  - [Other normalisation invariants](#other-normalisation-invariants)
- [Testing strategy](#testing-strategy)
- [Where to make changes](#where-to-make-changes)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

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
once, in `gather.sequencer._resolve_corpus_shape`, into one of four **kinds**:

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
  but explicitly skips even the group lookup. `paths.is_synthetic_wg()`
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
`group_backed` boolean (true only for the `group` kind). `corpus/identity.py`
derives `(kind, status)` from on-disk artifacts for `ietf-llm --list`
and the MCP `list_corpora` tool identically, so they can't drift. The
listing never shows a blank state — a `group` with no cached state shows
`unknown`, and a `list` / `custom` / `synthetic` corpus shows an explicit
`not a chartered group` / `not an IETF effort` so it can't be mistaken for a
WG — and carries a brief **subject** line (group name, the list it follows, or
the tracked author) so a consumer can tell what an opaquely-named corpus is
about without opening it. Both are network-free.

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
│   │   ├── digests/                      # index.md issues.md threads.md people.md
│   │   │                                 # timeline.md citations.md message_citations.md
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
│   ├── topics.json                        # topic-map sidecar (cluster centroids + labels; overview themes)
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
  is a flat year-dump (kept for grep / NotebookLM, excluded from the index).
  `threads/<date>-<slug>.md` is one file per reconstructed thread — built from
  RFC 5322 In-Reply-To / References headers with a normalised-subject safety
  net, quoted runs collapsed, and per-message headers in **UTC** (so the
  chunker's date re-parse sorts). `<date>` is the thread's *first* message, so
  it can lag the last-activity date; the overview surfaces the actual `File`
  path to bridge that. The thread files are what an LLM reads; the year files
  are for humans / external tools.
- **Per-issue files mirror per-thread files.** `issues/<repo>/<N>.md`
  is one GitHub issue with full comment history, same shape as a
  thread file (frontmatter carries duplicate-of and closing-rationale).
- **Identities are consolidated up front.** `people.Registry` merges the
  surface forms of one actor — DMARC-rewritten variants, relay addresses,
  multiple emails, GitHub logins — into one canonical `Person`, resolving
  addresses through Datatracker's curated email→person table (exact-match, so
  collision-free) and linking GitHub logins by each person's Datatracker
  `github_username` profile (`people.linking`). A `Person` carries
  **affiliations** and the set of **email domains** seen as distinct fields
  (email domain ≠ affiliation). Threads, issues, and github text all render
  authorship with these canonical names.
- **`digests/*.md` are deterministic, regenerated every gather.**
  `index`, `issues`, `threads`, `people`, `timeline`, `citations`,
  `message_citations`. Read them via the MCP `read_digest` / `overview`
  tools, not raw. `citations.md` is the draft → citing-thread/issue
  cross-reference; `message_citations.md` is the message → message graph
  (archive-permalink links resolved between gathered messages), read via
  `find_message_citations`.
- **`ballots/<draft>.md`** holds the current IESG ballot (latest
  position per AD, DISCUSS text inline) for drafts with ballot
  activity in the `--months` window.
- **`embeddings.db` is per-WG.** `meta` records the embedding model id, chunker
  version, and vector dimension; a change to any forces a rebuild, and the model
  id is read back at query time to resolve the same backend. The `chunks` table
  carries `start_line`/`end_line`, `chunk_date`, `labels`, `state`, `url`,
  `duplicate_of`, `closing_rationale` for faceted search. Not everything is
  embedded: `raw/`, `github/`, digests, and PDFs are excluded, and a draft's
  revision stack is skipped once its state is `rfc` (the RFC is canonical) or
  `repl` (content lives in its successor) — those revisions stay on disk for
  reading/citing, only embedding is gated.
- **`topics.json` is the topic-map sidecar**, written beside `embeddings.db`
  by the `topic map` gather stage. It clusters the corpus's per-file vectors
  into labelled themes (numpy k-means in `embeddings/clustering.py` — no
  scikit-learn, so the serve path stays torch-free), each theme carrying its
  centroid, a keyword label, and its most-central documents. `overview` renders
  the largest themes. It rides publish like the index and is **write-side** — a
  cache embedded before the topic map shipped has no sidecar until re-gathered,
  which `read_topics` / `overview` degrade past silently.
- **The centroids are reused, reader-side, for two cross-corpus jobs.**
  *Centroid routing* (`corpus/routing.py`, the `which_corpus` tool) scores each corpus
  as the max cosine of the query against its centroids and abstains below a
  floor — the entry point for "which gathered corpus is this about." *Generic-
  theme suppression* (`overview`) demotes a theme that recurs across much of the
  fleet (meeting logistics, ballots) as boilerplate rather than distinctive
  discussion. Both compare only within one embedding-model id (cosine isn't
  portable across backends) and on mean-centered vectors (sentence embedders are
  anisotropic). The cross-corpus centroid set comes through the
  `CorpusStore.routing_fleet_table` seam — a per-`topics.json` scan on local,
  one `fleet/routing/centroids.json` key (CAS-merged at `publish`) on cloud.
  Calibration constants and the `IETF_LLM_ROUTING_MIN_SCORE` override live in
  `corpus/routing.py`.
- **`imap-cache/<wg>/<list>/`** is the only place holding raw `.eml`
  files. Thread reconstruction walks that tree (two levels — one
  subdir per list, since a WG can follow several).
- **`last-gathered`** is an ISO-8601 sentinel `freshness.py` writes at
  the end of each gather; consumers surface staleness from it.
  `coverage.py` pairs it with the persisted `months` to report the
  *start* of the window (gather date minus the window) alongside a
  source inventory read from on-disk artifacts — both reader-side, so a
  client knows how far back a corpus reaches and what it holds without a
  re-gather. The window bounds mailing-list / meeting recency only;
  GitHub issues and drafts are the full set.
- **`gather-metrics.json`** records the upstream HTTP load of the last
  run — request counts (transferred / revalidated / error), bytes, a
  per-host breakdown, and a top-N of URL patterns. `net/http_metrics.py`
  accumulates it at the two egress chokepoints (`datatracker._get_json`
  and `net.fetch_resource`) and the CLI prints a one-line summary at
  the end of a gather. Beside the sentinel (one level above `files/`),
  so it is neither indexed nor exported.
- **`_rfc/` is a cross-corpus singleton, not a corpus.** It mirrors the
  whole published RFC series from rfc.fyi (three JSON blobs), refreshed
  once per gather run after the per-corpus work, TTL-guarded and
  best-effort (`gather/sources/rfcs.py`). The leading underscore keeps it out of
  `list_corpora` / `ietf-llm --list`, which enumerate real corpora. The
  `search_rfcs` / `get_rfc` tools read it; it is not embedded.
- **`_catalog/` is the matching singleton for active efforts.** It
  mirrors the active (and BoF) slice of the Datatracker group list,
  refreshed beside `_rfc/` in tail housekeeping with the same TTL / ETag
  / never-raises discipline (`gather/sources/catalog.py`). Unlike the RFC mirror
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

The split into two scoped files is deliberate: gather and export have
non-overlapping flag sets, so `ietf-llm <wg> --clear-config` (which wipes the
whole `<wg>/` dir) and a user reasoning about "how is this WG configured" each
see one coherent set. `config.json` is the **global** scope
(`config.merge_global`) for tool/deployment-wide settings that aren't
properties of a corpus (embedding model, embed on/off, summariser); it resolves
`env > CLI > global > default`, so a container's injected environment is
authoritative, and holds no secrets. See [models.md](models.md) for the
variable list.

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
(The cross-corpus `_rfc/` and `_catalog/` singleton mirrors sit beside this
per-`<wg>` flow — see "Cross-corpus singletons" below.)

## Package layout

```
ietf_llm/
├── __main__.py             # thin `python -m ietf_llm` shim over cli/main.py
├── cli/                    # every `ietf-llm*` console script's implementation
│   ├── main.py             # `ietf-llm` entry: argparse + dispatch; --list /
│   │                       # --completion / --install-skills; the --all loop.
│   │                       # The gather pipeline itself lives in gather/sequencer.py
│   ├── export.py           # `ietf-llm-export` entry point
│   ├── search.py           # `ietf-llm-search` entry point
│   ├── list.py             # corpus-listing helpers (`--list`; shared with MCP)
│   ├── completion.py       # shell tab-completion (argcomplete wiring for the ietf-llm CLIs)
│   └── skill_install.py    # --install-skills (multi-harness) + pristine-only auto-update
├── export.py               # mirror / NotebookLM export logic (used by cli.export)
├── mcp/                    # `ietf-llm-mcp` server (`mcp:main`), one module per tool domain
│   ├── server.py               # FastMCP construction + main() + tool registration
│   ├── common.py               # shared scaffolding (@_requires_corpus, _offload,
│   │                           # freshness/grounding/nudge helpers)
│   ├── serve.py                # HTTP transport, /health, /metrics, serve-config validation
│   ├── stdio.py                # threaded-writer stdio transport (sidesteps upstream blocking write)
│   ├── debug_log.py            # per-request telemetry ring buffer (IETF_LLM_DEBUG_LOG / get_session_log)
│   ├── corpus.py / search.py / digest.py / topic.py / chunks.py  # tool_* impls +
│   ├── citations.py / drafts.py / meetings.py / gather.py / norms.py / rfcs.py
│   │                           #   their `@server.tool()` wrappers, via `register()`
├── config/                 # configuration seam (per-WG + service/deployment)
│   ├── __init__.py         # generic per-WG, per-scope JSON config (merge/persist)
│   ├── fs.py               # filesystem primitives leaf (per-WG + global config.json)
│   ├── store.py            # ConfigStore seam: local + cloud (control-plane) per-WG config
│   └── service.py          # deployment knobs (store backend, …): env > global > default
├── corpus/                 # corpus resolution (identity / canonicalisation / routing);
│   │                       # a package so it no longer name-collides with store/corpus.py
│   ├── identity.py         # corpus kind/status + subject line (group/list/custom/synthetic)
│   ├── canonical.py        # steer a new gather toward an existing overlapping corpus (write)
│   └── routing.py          # which_corpus: centroid routing over the topic-map sidecars + fleet key
├── paths.py                # filesystem layout single source of truth: root dirs
│                           # (get_config/cache/index_dir) + per-artefact paths; meeting_label();
│                           # cached_wg_names / is_synthetic_wg (cache-dir listing predicates)
├── store/                  # storage seam: CorpusStore + backends (see "The storage seam")
│   ├── corpus.py           # CorpusStore seam: port + LocalCorpusStore + factory
│   ├── kv.py               # KvStore compare-and-swap seam + in-memory double
│   ├── control.py          # cloud control plane: pointer / lease / slot / status over KvStore
│   ├── kv_s3.py            # S3-backed KvStore (object-store conditional writes; [s3])
│   ├── blobs.py            # cloud blob plane: immutable whole-object store (file://)
│   ├── blobs_s3.py         # S3-compatible blob backend (AWS S3 / R2 / MinIO; [s3])
│   ├── s3.py               # shared S3Bucket: one boto3 client for blob + control planes
│   └── cloud.py            # CloudCorpusStore: composes control + blob; publish + read + seed
├── seed/                   # public seed store (local fast-start; see docs/seed-store.md, #182)
│   ├── format.py           # index/manifest JSON schema, compat tuple, bundle assembly + hashing
│   ├── fetch.py            # consumer: load index, download + verify + install a bundle (gather-path)
│   └── publish.py          # producer: build/refresh a static store from the cache (scripts/publish_seeds.py)
├── live_lookup/            # live Datatracker reads (meeting_schedule / draft_status /
│   │                       # overview reconciliation); gather-gated, the one networked read path
│   ├── cache.py            # TTL-cached fetch seam (_fetch_json) + in-proc/on-disk cache; age_stamp
│   ├── meetings.py         # a group's sessions at a meeting + its upcoming meetings
│   └── drafts.py           # per-draft live status + overview reconciliation
├── freshness.py            # last-gathered sentinel + staleness warnings
├── coverage.py             # reader-side window + source inventory (no network)
├── people/                 # actor/identity registry + position extraction
│   ├── __init__.py         # actor/identity registry (roles, affiliations, domains)
│   ├── linking.py          # attach GitHub logins to identities (Datatracker, then name)
│   └── positions.py        # heuristic position / poll / chair-statement extraction
├── notebooklm.py           # Google OAuth + Discovery Engine API
├── text.py                 # generic text helpers (subject norm, date, addr)
├── log.py                  # stderr output leaf: log(), Verbosity/LogLevel, _use_color
│                           # (text + IETF_LLM_LOG_FORMAT=json), graceful_keyboard_interrupt
├── months.py               # gather --months window policy (validate / resolve; DEFAULT_MONTHS)
├── atomicio.py             # concurrency-safe filesystem primitives: atomic writes
│                           # (atomic_open / write_if_changed) + advisory file locks
├── net/                    # gather-side HTTP egress (the offline read path imports none of it)
│   ├── transport.py        # pooled retrying session, host-governed GET,
│   │                       # metered fetch_resource, clean_html (re-exported from net/)
│   ├── http_governor.py    # per-host concurrency slots (datatracker kept tight)
│   └── http_metrics.py     # per-gather upstream HTTP egress accounting (thread-local)
├── datatracker_api.py               # IETF group metadata via the Datatracker API
│                           # (fetch_group_object + get_group_* + get_wg_title)
├── singletons/             # cross-corpus singleton readers (fleet-wide indexes, offline read side;
│   │                       # each the read twin of a gather/sources/ writer that mirrors into it)
│   ├── rfcs.py             # RFC-series reader (search_rfcs / get_rfc); reads _rfc/ (mirrored from rfc.fyi)
│   └── catalog.py          # active-effort reader (find_efforts); ranks _catalog/ by topic, tags cached
├── serve_metrics.py        # serve-side RED registry + Prometheus /metrics exposition (read side)
├── data/mcp-instructions.md              # the routing brain, served as the MCP `instructions` field
├── data/skills/ietf-interpreting/SKILL.md  # read-side norms (vendored), via read_ietf_interpretation_norms
├── data/skills/ietf-contributing/SKILL.md  # write-side norms (vendored), via read_ietf_participation_norms
├── data/skills/VENDORED.md               # provenance: norm skills vendored from mnot/ietf-skill
│
├── gather/                 # orchestration (below) + content acquisition / per-source post-processing
│   ├── runner.py               # in-session gather orchestration: queue/leases/heartbeat, gather-status.json
│   ├── pipeline.py             # runs the sequencer in a child process, streams its progress back
│   ├── child.py                # the child entry point (python -m ietf_llm.gather.child)
│   ├── stages.py               # stage_plan: canonical gather stage order (shared CLI ↔ runner)
│   ├── plan.py                 # gather-plan summary (dry-run preview)
│   ├── cli.py                  # build_parser: the `ietf-llm` gather argument parser
│   ├── sequencer.py            # _gather_one / run_gather: walk one corpus through the gather stages
│   └── sources/                # stage implementations: one module per thing gathered
│       ├── charter.py              # charter text artifact (rev from doc API)
│       ├── group_info.py           # group.md: name / status / area / Additional Resources
│       ├── drafts.py               # WG drafts + RFCs via doc API; --draft extras
│       ├── recent_drafts.py        # --new-drafts: -00 submissions in the window
│       ├── author.py               # --author: a person's authored drafts
│       ├── meetings.py             # minutes/agenda/slides via meeting API; clustering
│       ├── transcripts.py          # ietf-minutes-data repo; match to meeting clusters
│       ├── transcript_context.py   # prepend meeting-context header to transcripts
│       ├── mbox.py                 # IMAP fetch + per-year .txt; --mailing-list extras
│       ├── mail_threads.py         # reconstruct per-thread .md files
│       ├── github.py               # archive.json (gh-pages) or REST API
│       ├── github_users.py         # resolve logins → real name + company
│       ├── issue_files.py          # per-issue .md files
│       ├── datatracker.py          # roles + paginated document listing via JSON API
│       ├── json_store.py           # tolerant read + atomic write for the JSON manifests below
│       ├── materials_manifest.py   # materials.json: doc-name → rev last fetched (rev-gating)
│       ├── documents_manifest.py   # documents.json: draft-name → {expires, state} (overview + embed-skip)
│       ├── cache_sync.py           # cloud: round-trip gather accelerator caches to the KvStore (issue #82)
│       ├── _mirror.py              # shared singleton-mirror plumbing (TTL / conditional GET / sidecars)
│       ├── rfcs.py                 # ensure_rfc_index: mirror the rfc.fyi RFC-series JSON → _rfc/
│       ├── catalog.py              # ensure_catalog_index: mirror the Datatracker group list → _catalog/
│       ├── datatracker_history.py  # governance / doc-lifecycle timeline events
│       ├── datatracker_github.py   # github_username profile resources → person (by email)
│       ├── datatracker_people.py   # mail address → person id (mail-side identity spine)
│       ├── draft_authors.py        # parse Authors' Addresses (name + organization)
│       ├── ballots.py              # IESG ballot positions (scoped to --months)
│       ├── citations.py            # draft → citing thread/issue cross-reference
│       ├── pdf_extract.py          # extract text from slide PDFs
│       └── session_polls.py        # session polls (polls doctype) → JSON tallies
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
    ├── storage.py              # sqlite schema, vector packing, lookup, topics.json sidecar IO
    ├── models.py               # embedding-model loading + process-level cache
    ├── oai_compat.py           # OpenAI-compatible HTTP plumbing (auth, retry + Retry-After)
    │                           #   for the remote embed / summarise backends
    ├── snippet.py              # structure-aware snippet rendering for hits
    ├── clustering.py           # numpy mini-batch k-means (torch-free clustering primitive)
    ├── topics.py               # topic map: cluster docs into labelled themes (topics.json)
    └── search.py               # build_index() and search()
```

## MCP tool surface

The `ietf_llm.mcp` package registers each tool as a thin wrapper over a pure
`tool_*` function (so the logic is testable without MCP), with one module per
tool domain (`corpus`, `search`, `digest`, `topic`, …) plus `common` for the
shared scaffolding, `serve` for the HTTP/transport surface, and `server` for
construction + `main()`. The routing —
which tool for which question, with worked examples — lives in
`data/mcp-instructions.md` (below); this is just the map. Grouped by job:

- **Orient:** `list_corpora`, `overview`, `list_labels`, `list_files`, and the
  two on-demand norms tools `read_ietf_interpretation_norms` /
  `read_ietf_participation_norms` (served on demand so the always-on
  `instructions` field stays focused on routing).
- **Discover (topic-first):** `find_efforts(query)` ranks active IETF/IRTF
  efforts by topic and tags the already-gathered ones — the entry point when no
  corpus is named. Reads the `_catalog/` singleton, not a corpus.
- **Catalogue:** `read_digest(kind, …filters)` over
  issues/threads/people/timeline/index.
- **Search:** `search_corpus(query, …)` (faceted semantic search in one
  corpus), `search_corpora([…], query)` (the same across a bounded, explicit
  set — grouped by embedding-model id, since cosine scores aren't comparable
  across backends), and `find_related(file, chunk_idx)` (nearest-neighbour by a
  chunk's *stored* vector, so it needs no embedding backend; headline use is
  bridging a topic between the mailing list and a GitHub issue).
- **Narrative:** `read_topic`, `find_replies`, `tally_positions`,
  `find_citations`, `find_message_citations`.
- **Pivot / read:** `get_chunk_text`, `get_chunks_batch`, `get_by_url`,
  `read_file_section`.
- **RFC series (cross-corpus):** `search_rfcs(query)` / `get_rfc(number)` over
  the whole published series — the `_rfc/` singleton, not a corpus.
- **Live chair-workflow facts (gated, networked):** `meeting_schedule`,
  `draft_status`, and `overview(corpus, live=True)` read *live* from
  Datatracker, so they share the gather gate (see "The networked read
  exception" below); the offline cousin `draft_authors` is always registered.
- **Diagnostics (gated):** `get_session_log`, registered only under
  `IETF_LLM_DEBUG_LOG=1`.

Every wrapper offloads its blocking `tool_*` body to a worker thread
with a per-call deadline (`IETF_LLM_TOOL_TIMEOUT`, default 120s) so a
stuck call fails fast instead of hanging to the client ceiling, and
guards against an unknown corpus name (a read-only existence check, so a
typo neither creates a cache dir nor returns a hollow result).

The full routing brain (`data/mcp-instructions.md`) is handed to compliant
clients via the MCP server's `instructions` field, so every harness — Claude,
Codex, Gemini, Cursor, Zed, opencode — gets the same routing and the norms
gate with no separately-installed skill. (The two IETF *norms* skills can be
installed locally as a convenience, but routing always comes from the server.)

## Key design decisions

These are the ones worth knowing before you make changes.

### Cache is the only durable state

Every consumer reads from the cache. The cache is the contract. Adding
a consumer never requires touching gather; gather never has to know
who reads its output. This is the project's main architectural lever.

The cache is reached through a seam — the `CorpusStore` (`ietf_llm/store/`) —
so the *contract* generalises from "a local directory" to "whatever a
`CorpusStore` materialises locally". The local filesystem is the default
implementation and the only one the laptop CLI uses; a cloud deployment can
select a different backend without touching any consumer (see next).

### The storage seam: CorpusStore (local default, cloud-pluggable)

`CorpusStore` is a *coarse* seam, not a per-file I/O layer: it answers two
questions and otherwise stays out of the way. **Read side** — `local_cache_dir
(corpus)` returns a local directory for the corpus's current version, which
consumers then read through the `paths.py` helpers exactly as before. **Write
side** — `publish(corpus, workspace)` makes a gathered tree the new current
version. `get_corpus_store()` picks the backend from service config
(`IETF_LLM_STORE_BACKEND`, default `local`).

Per-WG **config** rides a *sibling* seam, `ConfigStore` (`config/store.py`,
`get_config_store()`), chosen by the same `IETF_LLM_STORE_BACKEND` selector but
kept separate from `CorpusStore` on purpose. Content is immutable, versioned, and
materialised; config is small, mutable, and last-writer-wins — different planes,
so a three-method contract (`load` / `save` / `clear`) rather than more methods on
an already-wide content seam. The local backend is today's filesystem
(`config/fs.py`, a leaf module); the cloud backend stores per-WG config as
control-plane keys (`corpora/<name>/config/<scope>`), composing the *same*
`KvControlPlane` — so a fleet shares config with no `IETF_LLM_CONFIG_DIR` mount,
and `--all` re-gathers a corpus first gathered elsewhere with its real sources.
The split also reflects a layering constraint: **global** service config selects
the backend (`config.service` reads it), so it is structurally filesystem/env-bound
and stays out of any store — only *per-WG* config moves. Writes ride the gather
lease the caller already holds, so a plain put suffices; reads stay read-only
(GET only) and, on the cloud backend, sit behind the same bounded-staleness TTL
cache as the version pointer (`IETF_LLM_RESOLVE_TTL`), so the coverage-window line
on every read-tool response doesn't re-fetch the key — the writing process
write-throughs its own entry, so it never serves config it just wrote.

- **`LocalCorpusStore`** (default) — the live `~/.cache/ietf-llm/<corpus>` *is*
  the single version: `resolve_current` is a sentinel, `local_cache_dir` is the
  existing files dir, `publish` is a no-op finalise, the gather lease is a no-op
  grant. The laptop CLI is unchanged.
- **`CloudCorpusStore`** — composes a **control plane** (`KvControlPlane`: the
  per-corpus version pointer, gather lease, fleet gather-slot, gather status, and
  the read-path access marker) and an immutable **blob plane** (`BlobStore`:
  whole-object, versioned-prefix),
  materialising a version onto local scratch for reads (a replica reaps
  superseded versions to keep scratch bounded). `publish` stages content blobs —
  and the manifest, itself a blob — to a fresh version prefix, then flips the
  pointer with a single compare-and-swap; a reader sees the old version or the
  new, never a torn one, and a killed publish leaves the prior version live.
  After the flip — under the gather lease, best-effort — it reaps superseded
  version blobs (and any failed-publish orphan), keeping the current version plus
  the previous one (`IETF_LLM_RETAIN_VERSIONS`, default 2), so durable storage is
  bounded by the application, not a bucket lifecycle rule. Keeping the *previous*
  version preserves never-torn-read: a replica re-resolves the pointer every
  resolve-TTL while publishes are hours apart, so it is at most one version
  behind. If a concurrent re-gather does reap the version a cold read resolved,
  the read re-resolves cache-bypassing and retries on the new version (unpinned),
  or raises a typed `VersionVanished` that the tool wrapper turns into a one-shot
  retry of the whole call on a fresh pin (pinned); a *same*-version failure is
  treated as genuine data loss and surfaced, not masked.
  Symmetrically, `seed_workspace` materialises the
  current version into the *gather* workspace before a gather runs, so a
  re-gather on a fresh replica builds on prior output — skipping re-download of
  immutable inputs and, via the content-hash index above, re-embedding of
  unchanged files — instead of starting cold; a no-op on the local backend
  (where the workspace already is the live cache), and a seed failure degrades
  to a full gather rather than failing it. Alongside the seed, `hydrate_gather_caches` /
  `persist_gather_caches` (`gather/sources/cache_sync.py`) round-trip the gather
  accelerator caches through the `KvStore` — the datatracker ETag store sharded
  per corpus (`corpora/<name>/gather-cache/`, lease-serialised plain RMW), the
  shared GitHub/datatracker identity maps (`fleet/gather-cache/`, CAS-merge), and
  the effort catalog (`fleet/catalog/`, a shared fleet singleton, last-writer-wins)
  — so an ephemeral host revalidates rather than re-hitting rate-limited upstreams
  after scale-to-zero (issue #82); no-ops on local, all best-effort. The large
  caches stay ephemeral by design: `imap-cache/` is per-corpus and its mail is
  already published as version content, and `_rfc/` mirrors a CDN rather than a
  rate-limited API (a restart re-fetches it for bandwidth, not API quota), so only
  the rate-limited, shared, or per-corpus-lease-serialised caches above are
  round-tripped.
  Both planes are **object-store only**. The control plane is the only
  linearizable, cross-host state, and it holds **no corpus content** (that lives in
  the version blobs): every *control* key is either a *published fact* (pointer,
  manifest, status, the read-path access marker, per-WG config — last-writer-wins)
  or an *ephemeral TTL lock* (lease, slot — compare-and-swap), with no joins, range
  scans, or secondary indexes. The `corpora/<name>/access` marker is the one
  control key written off the **read** path (a coarse, best-effort timestamp the
  `--used-within` refresh filter consumes); last-writer-wins is safe because its
  value is just "the most recent access any replica saw", and it touches no
  version, blob, or pointer — so a serving replica stays read-only with respect to
  corpus *content* (`IETF_LLM_RECORD_ACCESS=off` disables it; see
  [storage.md](storage.md#read-path-access-recording)). (The same
  `KvStore` also carries the gather accelerator caches above —
  `corpora/<name>/gather-cache/`, `fleet/gather-cache/`, `fleet/catalog/` — but
  those are a separate, gather-only, mutable-data use of it, not control state, and
  are never read on the serve path.) That is why
  an object store suffices and no database is needed. It is a `KvControlPlane`
  over a small **`KvStore`** seam — `get`, `put` with an optional precondition,
  `delete`, `list_children` — which is exactly what an object store with
  conditional writes (`If-Match` / `If-None-Match`) provides natively. Every
  control-plane operation is one `get` plus one conditional `put` (the lease is a
  read + compare-and-swap; the fleet semaphore a bounded CAS loop on one key), and
  release needs only a conditional `put` — a lease is freed by stamping it expired,
  never a conditional `delete`. The
  control plane and the blob plane share one S3-compatible bucket — `S3KvStore`
  and `S3BlobStore` over one `S3Bucket` (one client, endpoint, credential set);
  tests use an in-memory KvStore plus `file://` blobs. Bucket layout: per-corpus
  control under `corpora/<name>/{pointer,lease,status}`, immutable content (and
  its manifest) under `corpora/<name>/versions/<version>/`, and the one
  cross-corpora key — the gather-slot semaphore — at `fleet/slots`. The program
  stays the storage client (no FUSE); all atomicity lives in the pointer's
  compare-and-swap. Reads resolve
  the current version through the control plane behind a
  short per-replica TTL cache (`IETF_LLM_RESOLVE_TTL`, default 10s), so a burst of
  reads coalesces to one round trip; immutable versions make a stale hit harmless,
  and a publish refreshes the publishing replica immediately. Operator setup:
  [storage.md](storage.md).

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
  (`datatracker_api.fetch_group_object`), plus the "Additional Resources"
  (repos / home page / chat / alternate archives) from
  `/api/v1/group/groupextresource/` (`datatracker_api.get_group_resources`).
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
`net.clean_html` cleans the MCP `get_by_url` tool's arbitrary
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
bytes actually changed (`atomicio.write_if_changed`). A byte-identical
re-render leaves the file (and its mtime) untouched, avoiding needless
I/O and churn. The embedder keys its incremental skip on each file's
content hash — stable across hosts, so a cloud replica that materialises
a published version onto fresh local files still recognises the bytes as
already-embedded rather than re-embedding the whole corpus. The hash
check alone can't catch a file that becomes *ineligible* without
changing — a removed
thread/issue, or a draft that flips to `rfc`/`repl` and is now skipped
(see `embeddings.db` above) — so `build_index` opens with a prune: it drops chunks
for any indexed file no longer in the eligible set. That keeps stale
chunks from lingering and doubles as the migration path when the
eligibility rules change (an existing cache sheds the now-skipped
revisions on its next gather, no `--rebuild` needed).

Every-gather corpus writes also go through `atomicio.atomic_open` (temp +
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
  clone is guarded by `atomicio.file_lock` (flock) so concurrent gathers
  serialise their clone/pull. The JSON side-caches
  (`.http-cache.json`, `materials.json`, `_github-users.json`,
  config) are written temp + rename; concurrent writers are
  last-writer-wins, which at worst costs a redundant re-fetch, never a
  corrupt file.
- **Cross-host writers (cloud backend).** The `file_lock` above serialises
  gathers on one host; *across* hosts (a cron gather and the serve fleet's
  in-session gather) the cloud backend's per-corpus **gather lease**
  (`KvControlPlane`, owner + TTL) is the mutual-exclusion primitive, and
  publish (a compare-and-swap pointer flip over immutable, already-staged blobs)
  replaces shared-filesystem atomic writes for cross-object atomicity. Both are
  no-ops on the local backend, which relies on the flock as before.

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
no API key, CPU by default — see *Embedding device* below), but it lives
behind the optional `local-embeddings` extra (torch is not in the base
install). The
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

**Embedding device.** The local backend runs on **CPU by default**, not MPS.
PyTorch's MPS caching allocator fragments badly on variable-length inputs:
indexing one modest corpus (bge-small, ~11k chunks spanning ~50–50000 chars)
drove the process footprint to ~11 GB on Apple Silicon — enough to push a
co-resident reader into memory pressure and stall it — while CPU held ~1–2 GB
for the same work at a ~15–30% time cost. The vectors are numerically
equivalent across the two devices (max abs diff ~3e-7, min cosine 0.99999982),
so the choice needs no re-embed and the recorded model id is unchanged. CUDA
has no such pathology and is still auto-selected. `_embed_device` is the choke
point; `IETF_LLM_EMBED_DEVICE` overrides (`cpu` / `mps` / `cuda`). This is a
workaround for the upstream PyTorch MPS memory bug (pytorch/pytorch#77753,
#164299) — **revisit and drop back to MPS if that is fixed.**

**Embedding pass.** The on-device build streams chunks through a bounded
length-sort buffer (`_stream_embed`) rather than embedding file by file:
chunks are pooled across files up to `_EMBED_BUFFER`, embedded in one call so
sentence-transformers length-sorts a wide window (tight padding — ~20% faster,
and on MPS far less allocator fragmentation), then written per file as they
complete. Memory is bounded by the buffer, not the corpus; each window commits
(crash-durable); and progress is byte-weighted against the pending-files byte
total (a truer cost proxy than a file count, known without pre-chunking).
Batching order and padding do not change vectors, so this needs no re-embed
and existing indexes stay valid. The remote backend keeps its per-file network
fan-out (`workers` sizes that pool; on-device does not use it).

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
`mcp/stdio.py`, which sidesteps an upstream loop-blocking write).
Setting `IETF_LLM_MCP_TRANSPORT=http` serves standard MCP **Streamable
HTTP** instead — FastMCP's `streamable_http_app()` under uvicorn, with
`GET /health` (readiness) and `GET /metrics` (Prometheus scrape) routes
added — for a shared deployment serving many clients from one process.
Concurrency is safe because every tool opens its own read-only sqlite
connection per call (`_connect_ro`, never shared across `anyio` worker
threads) and the index is queried read-only (no migrations on the serve
path).

For a hosted deployment the serve path adds the operational surface an
operator needs — structured JSON logs (`IETF_LLM_LOG_FORMAT=json`, no secrets),
a per-request access record at `status` verbosity, `GET /health` (readiness +
build version + `gathers_inflight` + a bounded freshness summary), and a
hand-rolled, zero-dependency Prometheus `GET /metrics` (RED per tool, the remote
embedding backend, the corpus-store seam, in-session gather, and process
gauges). The metrics registry is process-global and accumulates harmlessly on
stdio, where nothing scrapes it. The field-by-field contract is the operator
runbook's job — see [mcp-server.md](mcp-server.md).

The HTTP serve path also runs **boot-time config validation** before binding
(`_validate_serve_config`) — upfront validation over wait-then-fail. It
**hard-refuses** (exits 1) only configs that cannot work (gather against an
index-immutable mount; a local torch model on a torch-free image; a remote
embed model with no base URL), **warns but never blocks** on a non-loopback
bind (the trust boundary is the operator's call), and **always** logs a
one-line posture banner so the logs answer "what is this process actually
doing."

### The one writer exception: in-session gather

The server registers `start_gather` / `gather_status` so a client can gather
without leaving the session — deliberately breaking the read-only / no-network
contract, so the **default tracks the transport** (`set_gather_default`): **on**
for a local stdio server (that user can already run `ietf-llm` against the same
cache, so withholding it only adds friction), **off** for the shared HTTP
replica (which must stay read-only). `IETF_LLM_ENABLE_GATHER` overrides either
way. The registration gate and the user-facing "go gather" hints read the one
resolved value (`freshness.gather_enabled`), so the tool is registered iff the
hints recommend it — they can't drift. `start_gather` runs the same `gather.sequencer`
pipeline as the CLI, never bounded by the per-call tool deadline (only the
caller's optional `wait` is); progress lands in a per-corpus `gather-status.json`
that `gather_status` reads back, stage-level via the shared `stage_plan`. A
non-blocking per-corpus `file_lock` allows one gather per corpus (serialising
against a concurrent CLI gather) while different corpora run in parallel.
`gather.runner` and the pipeline are imported lazily so the default serve path
never pulls them in.

A **daemon worker thread** (`gather.runner`) owns the queue, leases, heartbeat,
and status record, but the pipeline itself runs in a **child process**
(`gather.pipeline` → `python -m ietf_llm.gather.child`), streaming stage progress
back to the worker over a pipe. This is deliberate: the embedding-index build is
CPU-bound, and in-thread it held the GIL long enough to starve the server's
event loop — so a read stalled during that stage (even the server's own tool
deadline couldn't fire) and "still gathering" was indistinguishable from "server
dead". Off-process, the worker only blocks on the pipe (GIL released) and the
server stays responsive; a stalled read now times out cleanly, and its timeout
message names the running stage. Cancellation (`stop_gather`) terminates the
child; a child crash fails one gather instead of the whole server. Under
`IETF_LLM_GATHER_INPROCESS` the pipeline runs in-thread instead (the test suite,
which stubs it by monkeypatch). Reads during a **first** gather refuse (naming
the stage and how many are left) since there is no prior snapshot; a re-gather
keeps serving the previous published version until the new one is ready. That
last guarantee needs the index build to be **atomic**: the build writes a scratch
DB (seeded from the live index, so it stays incremental) and `os.replace`s it
into place as a standalone (DELETE-journal) file, so a reader never sees a
half-populated index — see "Writers are write-if-changed and atomic".

### The networked read exception: live Datatracker lookups

A narrower break from the no-network contract: `meeting_schedule`,
`draft_status`, and `overview(corpus, live=True)` read **live** from Datatracker
(`live_lookup/`) because meeting schedules and IESG states change daily, so an
agenda built on the (often days-stale) gather cache is wrong at the edges. A
small TTL cache (`IETF_LLM_LIVE_TTL`, default 300s) is in-process plus a
best-effort `.live-cache.json` on disk — the *only* thing this path writes,
no-op on a read-only mount — and every result carries its UTC fetch time. It
reuses the **same gate** as gather (`freshness.gather_enabled`): a networked
read tool belongs on the same side of the line as the networked writer, so both
default on for stdio and off for the HTTP replica. Imported lazily (so the
default read path pulls in neither it nor `requests`), torch-free, and always
via the Datatracker REST API, never a scraped page. Its offline cousin
`draft_authors` needs no network and is always registered.

### IMAP cache lives outside the per-WG directory

`imap-cache/<wg>/<list>/` rather than under `<wg>/`: the raw `.eml`
store is expensive to refetch and shouldn't be lost when a WG's
exported `files/` are cleared. Thread reconstruction walks it directly.

### Cross-corpus singletons: the RFC series and the effort catalog

Two questions no single gathered corpus can answer — "find/status of any RFC"
and "what is the IETF doing around X?" — are served by **singleton mirrors**
rather than folded into every corpus. `_rfc/` (mirrored from rfc.fyi's
canonical JSON; read by `search_rfcs` / `get_rfc`) and `_catalog/` (the active
+ BoF slice of the Datatracker group collection; read by `find_efforts`) both
live once at the cache root, refreshed in the same tail housekeeping as each
gather run and sharing one mirror plumbing (`gather/sources/_mirror.py`: TTL guard,
`If-None-Match` revalidation, and **never-raises** — a mirror hiccup must not
fail a corpus gather). The read sides (`rfcs.py`, `catalog.py`) are read-only,
offline, markdown-out — the same boundary as every other tool — and query the
JSON directly rather than through the vector store. The catalog's reader blob
is *derived* (raw slices kept for revalidation, projected to a slim record
list); the RFC mirror is used as-is. v1 catalog covers active groups only;
concluded efforts surface through `search_rfcs`, already-cached ones through
`list_corpora`.

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
faceted search (with a stub model); nearest-neighbour-by-example
(`find_related`) and MMR diversification (with a keyword stub that can
actually rank); the MCP tools (`overview`, `read_topic`, `find_replies`,
`tally_positions`, `find_citations`, `find_message_citations`, search
facets, `read_file_section` caps); positions/poll heuristics;
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

- **New gather source** → add a module under `gather/sources/`, hook into
  `gather/sequencer.py`'s pipeline (`_gather_one`) before the digest step. Skip it for
  synthetic (`x-`) WGs if it's Datatracker-backed.
- **New digest** → add a builder under `digest/`, call it from
  `generate_digests()`, export from `__init__`.
- **New MCP tool** → add a pure `tool_*` function and its thin
  `@server.tool()` wrapper to the matching `ietf_llm/mcp/<domain>.py` (put
  new-domain shared helpers in `mcp/common.py`), registering the wrapper in
  that module's `register()`. Document the routing in
  `data/mcp-instructions.md` (served as the `instructions` field).
  A tool that writes or reaches the
  network (like `start_gather`) must be registered behind the gather gate
  (`_gather_enabled`, off for the shared HTTP replica) — via a `register_live()`
  hook that `main()` only calls when the gate is open — run its work
  off-thread, and be imported lazily so the read-only serve path stays clean.
- **New chunker** → add to `embeddings/chunking.py`, dispatch in
  `_chunk_file()`.
- **New persisted flag** → add it to the right scope's `scalars` or
  `lists` tuple in the `config.merge()` call.
- **New cache path** → add a helper in `paths.py`; never hardcode the
  layout elsewhere.
