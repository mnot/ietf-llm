# Gathering a corpus

**This document is for:** gathering a corpus of IETF materials into the local cache — the shared
first step for every workflow. — Back to the [docs index](README.md).

See the [reference](reference.md) for the full flag set, and
[shell completion](shell-completion.md) to tab-complete commands and cached corpus names.

## The basics

**First run:** Install the package, then gather a group. For example:

```bash
ietf-llm netconf
```

Everything lands in `~/.cache/ietf-llm/<name>/`.

> **From an MCP client:** when in-session gather is enabled, an assistant can gather
> via the `start_gather` tool and watch it with `gather_status`, instead of you running
> `ietf-llm <name>` in a shell. A local [stdio server](mcp-local.md) has it on by default;
> the shared [HTTP server](mcp-server.md) defaults to off.

Gathering also builds a **semantic-search index**. By default it uses
[`BAAI/bge-small-en-v1.5`](https://huggingface.co/BAAI/bge-small-en-v1.5) on-device, which needs
the `local-embeddings` extra (~130 MB, downloaded once and cached). The download is the only
time HuggingFace is contacted: once the weights are in `~/.cache/huggingface/` they are loaded
from there alone, so searching a gathered corpus works offline.

Prefer not to run a model locally? Omit `[local-embeddings]` and point at a remote
OpenAI-compatible endpoint instead — see the [remote embedding backend](models.md). Or pass
`--no-embed` to skip the index entirely (useful for [NotebookLM export](notebooklm.md)).

**Subsequent runs:** the flags you passed are persisted to `~/.config/ietf-llm/`, so to refresh
just run `ietf-llm <name>` — no need to repeat `--github`, `--mailing-list`, etc. Pass extra flags
to add to the set, or `--clear-config` to start over. (The embedding and summariser settings are an
exception — they're properties of the tool, not a corpus, so they're set once and apply everywhere;
see the [global model settings](models.md#global-settings).)

**GitHub authentication:** Set `GITHUB_TOKEN` in the environment for gathers if you hit the
anonymous 60-requests/hour API limit. A fine-scoped read-only token is plenty.


## Identifying what to gather

A corpus doesn't have to be a Working Group — the name is classified automatically:

| Command | Corpus |
|---|---|
| `ietf-llm httpbis` | a WG / RG / editorial WG / BoF: charter, drafts, meetings, ballots, list |
| `ietf-llm last-call` | a standalone mailing list (any archived at mailarchive.ietf.org — IETF, IRTF, or RFC-Editor) |
| `ietf-llm new-ids --new-drafts --months 1` | new Internet-Drafts in a rolling window |
| `ietf-llm mnot --author mnot@mnot.net` | a person: their drafts, the reviews they wrote, and their mail (in its threads) |

For most groups, mailing lists and GitHub repositories are automatically discovered. If you need
to add additional lists, use `--mailing-list`; if you need to add issues lists from other GitHub repos, use `--github`. The `--months` flag controls the timeframe of materials that is gathered; it defaults to 12 months. See the [reference](reference.md) for more information.


## Keeping a set of corpora fresh

`ietf-llm --all` re-gathers every corpus the store knows about.

To avoid refreshing unused corpora, add `--used-within DAYS`: it limits the refresh to those read
within that many days through the MCP read tools, so a daily cron job can keep your active corpora
fresh and lets the rest go stale:

```bash
ietf-llm --all --used-within 30
```

