# Gathering a corpus

**This document is for:** gathering an IETF corpus into the local cache — the shared first step for
every workflow. — Back to the [docs index](README.md).

Every workflow starts here: gather a corpus once, then read it as often as you like.

**First run:** Install the package, then gather:

```bash
pipx install 'ietf-llm[local-embeddings]'
ietf-llm httpbis --github httpwg/http-core --github httpwg/http-extensions
```

Everything lands in `~/.cache/ietf-llm/<name>/` — the single source of truth that the MCP server,
NotebookLM exporter, and CLI search all read from.

Gathering also builds a **semantic-search index**, which powers both the MCP `search_corpus` tool
and `ietf-llm-search`. By default it uses
[`BAAI/bge-small-en-v1.5`](https://huggingface.co/BAAI/bge-small-en-v1.5) on-device, which needs
the `local-embeddings` extra (~130 MB, downloaded once and cached).

Prefer not to run a model locally? Omit `[local-embeddings]` and point at a remote
OpenAI-compatible endpoint instead — see [Embedding backends](embedding.md). Or pass
`--no-embed` to skip the index entirely (useful for [NotebookLM export](notebooklm.md)).

**Behind a corporate firewall** with TLS interception? Gathering may need the `certs` extra:

```bash
pipx install 'ietf-llm[local-embeddings,certs]'
```

**Subsequent runs:** the flags you passed are persisted to `~/.config/ietf-llm/`, so to refresh
just run `ietf-llm <name>` — no need to repeat `--github`, `--mailing-list`, etc. Pass extra flags
to add to the set, or `--clear-config` to start over. (The embedding and summariser settings are an
exception — they're properties of the tool, not a corpus, so they're set once and apply everywhere;
see [Embedding backends](embedding.md#global-settings).)

A corpus doesn't have to be a Working Group — the name is classified automatically:

| Command | Corpus |
|---|---|
| `ietf-llm httpbis` | a WG / RG / editorial WG / BoF: charter, drafts, meetings, ballots, list |
| `ietf-llm last-call` | a standalone mailing list (any archived at mailarchive.ietf.org — IETF, IRTF, or RFC-Editor) |
| `ietf-llm rfced --mailing-list rswg@rfc-editor.org` | a named list corpus (the address domain is optional) |
| `ietf-llm new-ids --new-drafts --months 1` | new Internet-Drafts in a rolling window |
| `ietf-llm mnot --author mnot@mnot.net` | every draft a person has authored |

See the [command & gather reference](reference.md) for the full flag set, and
[shell completion](shell-completion.md) to tab-complete commands and cached corpus names.
