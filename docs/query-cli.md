# Query from the CLI

**This document is for:** running read-only queries over a gathered corpus from the command line
with `ietf-llm-query`, with no MCP client or LLM involved. — Back to the [docs index](README.md).

`ietf-llm-query` exposes the same corpus-read operations the [MCP server](mcp-local.md) offers as
tools — orientation, search, digests, verbatim reads, meeting minutes, draft state — as plain
subcommands that print to stdout. It exists so a portable
[Agent Skill](https://agentskills.io), a script, or a shell user can drive a corpus without
configuring MCP. It is read-only: every verb reads only the local cache, and none writes corpus
content. (The live verbs reach Datatracker and keep a small response cache; nothing else touches the
network or disk.)

## Installing

```bash
pipx install ietf-llm
```

The offline and live verbs need no extra. The semantic-search verbs (`search`, `search-corpora`,
`read-topic`, `which-corpus`) embed the query, which needs either the `local-embeddings` extra
(`pipx install 'ietf-llm[local-embeddings]'`) or a [remote endpoint](models.md). Then
[gather a corpus](gathering.md).

## Verbs, by network tier

Verbs fall in three tiers by what they touch. A skill or script should treat the tiers differently:
the offline verbs always work; the others can fail when a backend is unreachable, with a distinct
exit code (below).

**Offline** — pure cache reads, no network:

| Verb | What it does |
|---|---|
| `list-corpora` | corpora gathered locally |
| `overview <corpus>` | orientation: chairs, drafts, themes, top issues, recent threads |
| `read-digest <corpus> --kind <k>` | filtered catalogue (index / issues / threads / people / timeline) |
| `list-labels <corpus>` | curation vocabulary (GitHub labels + mail subject prefixes) |
| `list-files <corpus> [--pattern]` | file inventory with chunk counts |
| `list-sessions <corpus>` | gathered meeting sessions: code, date, artifacts |
| `read-minutes <corpus> [code]` | minutes for a session, with any poll tallies |
| `draft-state <corpus> [--state]` | offline draft lifecycle (active / expired / RFC / replaced / withdrawn) |
| `tally-positions <corpus> <file>` | chair statements and rough position counts for one thread/issue |
| `find-efforts <query>` | rank active IETF/IRTF efforts by topic |
| `rfc-search <query>` | search the published RFC series |
| `get-rfc <number>` | full metadata for one RFC |
| `draft-authors <name>` | authors/editors of a draft |
| `fetch-by-url <corpus> <url>` | resolve an archive/datatracker/github URL to cached text |
| `get-draft <name>` | verbatim text of a cached draft (bounded, pageable) |
| `get-issue <corpus> <number> [--repo]` | verbatim text of one GitHub issue |

**Embedding** — embed the query; network only when the embedding backend is remote:

| Verb | What it does |
|---|---|
| `search <corpus> <query>` | semantic search within one corpus |
| `search-corpora <query> --corpus C …` | semantic search across several named corpora |
| `read-topic <corpus> <query>` | chronological narrative across threads/issues for a topic |
| `which-corpus <query>` | route a question to the best-matching cached corpus |

**Live** — reach Datatracker every call (cross-process TTL-cached, so repeated calls stay polite):

| Verb | What it does |
|---|---|
| `draft-status <name>` | live draft process state — WG Last Call, IESG evaluation, RFC |
| `meeting-sessions <corpus> [id]` | upcoming / scheduled sessions |
| `overview <corpus> --live` | orientation, reconciling active drafts against Datatracker |

For authoritative draft process state use the live `draft-status`; the offline `draft-state` is the
no-network fallback and knows only the coarse lifecycle, not WG Last Call or IESG state.

## Exit codes

A stable contract — a caller may branch on these:

| Code | Meaning |
|---|---|
| `0` | success — **including an empty result** (no matches, an abstaining `which-corpus`, a missing draft); the body says so in prose |
| `2` | usage error (bad arguments) |
| `3` | corpus not present locally — gather it first with `ietf-llm <corpus>` |
| `4` | embedding backend unreachable (the embedding-tier verbs) |
| `5` | Datatracker unreachable (the live verbs) |

Codes `3`–`5` are distinct so a caller can react without parsing the message — e.g. route a missing
corpus to a gather, or fall back from a live verb to its offline cousin. There is deliberately **no
distinct "empty" code**: a verb that finds nothing still succeeded (exit `0`), and detecting
emptiness reliably would mean parsing the rendered body — the thing these codes exist to avoid.

`ietf-llm-query --version` prints the plain version string.

## Choose one path, not both

For a given workflow use **either** skills + `ietf-llm-query` **or** the [hosted HTTP MCP](mcp-server.md),
not both — they are two front doors onto the same corpus, and running both duplicates results and
guidance. The CLI is the portable path (any skills-capable agent, no MCP wiring); the hosted MCP is
the shared, read-only deployment. They are not symmetric: the live verbs are available on the CLI
but stay gated off on the hosted HTTP surface, which is deliberately read-only and offline.

## Relationship to the other read paths

`ietf-llm-query`, `ietf-llm-mcp`, and `ietf-llm-search` are three thin adapters over one shared
layer of corpus-read functions — see [Architecture](architecture.md). `ietf-llm-search` remains the
focused semantic-search tool; `ietf-llm-query` is the broader read surface that mirrors the MCP
tools.
