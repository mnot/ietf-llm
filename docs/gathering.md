# Gathering a corpus

**This document is for:** gathering a corpus of IETF materials into the local cache, the first step
for every workflow. Back to the [docs index](README.md).

Every flag is in the [reference](reference.md); [shell completion](shell-completion.md) completes
commands and cached corpus names.

## The basics

Install the package (`pipx install 'ietf-llm[local-embeddings]'`; see
[Running the MCP server locally](mcp-local.md)), then gather a group:

```bash
ietf-llm netconf
```

Everything lands in `~/.cache/ietf-llm/<name>/`.

An MCP client can also gather, with the `start_gather` and `gather_status` tools. A local
[stdio server](mcp-local.md) has them on by default; the shared [HTTP server](mcp-server.md) does
not.

Gathering also builds a **semantic-search index**, by default with the on-device
[`BAAI/bge-small-en-v1.5`](https://huggingface.co/BAAI/bge-small-en-v1.5) model (the
`local-embeddings` extra). The weights download once; after that, searching works offline. To use a
remote endpoint instead, see [Model backends](models.md). `--no-embed` skips the index entirely,
which suits a [NotebookLM export](notebooklm.md); it is remembered globally, and `--embed` undoes it.

**Later runs.** The flags you pass are saved in `~/.config/ietf-llm/`, so a refresh is just
`ietf-llm <name>`. Extra flags add to the saved set, and `--clear-config` starts over. The embedding
and summariser settings are the exception: they apply to every corpus (see the
[global model settings](models.md#global-settings)).

**GitHub rate limits.** Set `GITHUB_TOKEN` in the environment if you hit the anonymous
60-requests/hour limit. A read-only token is enough.

## The RFC series, which no gather brings

The RFC tools (`search_rfc_index`, `search_rfc_text`, `get_rfc_info`, `get_rfc_section`, and
`grep_corpus` on `rfcs`) cover every published RFC and belong to no corpus. Their data arrives as
housekeeping after any gather: the RFC metadata mirror, the effort catalog, and the full text of the
series from the [seed store](seed-store.md).

A machine that only reads never gathers, so it never gets that data. `--init` does the housekeeping
on its own and exits:

```bash
ietf-llm --init
```

It downloads about 285 MB (about 650 MB on disk once installed), gathers nothing, is safe to
re-run, and needs a writable cache. If it reports the full text as `NOT installed`, it gives the
reason (seeding is off, no seed store is configured, the store is unreachable, or a TLS certificate
can't be verified). `--init --seed` re-enables seeding first, if an earlier `--no-seed` turned it
off.

## Identifying what to gather

The name is classified automatically:

| Command | Corpus |
|---|---|
| `ietf-llm httpbis` | a WG / RG / editorial WG / BoF: charter, drafts, meetings, ballots, list |
| `ietf-llm last-call` | a standalone mailing list archived at mailarchive.ietf.org |
| `ietf-llm new-ids --new-drafts --months 1` | new Internet-Drafts in a rolling window |
| `ietf-llm mnot --author mnot@mnot.net` | every draft a person has authored |

A group's own mailing list and active GitHub repositories are discovered automatically. Add lists
with `--mailing-list` and repositories with `--github`. `--months` sets how much history to gather
(default 12). See the [reference](reference.md).

## Keeping a set of corpora fresh

`ietf-llm --all` re-gathers every corpus the store knows about. On the cloud backend
([storage.md](storage.md#the-cloud-backend)) each corpus is published as it finishes, and one that
another host is already gathering is skipped. A corpus that fails is logged and the sweep carries
on; the exit status is non-zero if any corpus failed or had an unusable name. `--clear-cache` and
`--clear-config` are refused with `--all`.

`--used-within DAYS` limits the sweep to corpora read through the MCP tools in that many days, so a
daily cron job keeps active corpora fresh and lets the rest go stale:

```bash
ietf-llm --all --used-within 30
```
