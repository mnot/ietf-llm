# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/) effort's public record — a
Working Group, an [IRTF](https://irtf.org/) Research Group, a mailing list, or a set of drafts. It
pulls together charter, drafts, RFCs, meeting agendas, minutes, slides, transcripts, attendance,
mailing list archives, and GitHub issues and pull requests for use with LLM-based tools.

## What it's for

A working group's history is spread across mailing list archives, Datatracker, GitHub, and meeting
materials — too much to hold in your head, and too scattered to search well by hand. With the record
gathered into one queryable corpus, an LLM can help you:

- **Get up to date with the state of discussions** — what's open, what was recently decided, where a
  debate currently stands.
- **Summarise the arguments already made** about an issue — every distinct position on a topic, who
  holds it, and how the chairs ruled.
- **Formulate a new proposal** — surface the objections raised against similar ideas before, so you
  can anticipate them.
- **Fact-check assertions** about what's happened so far — grounded in the actual list traffic and
  chair statements, not someone's recollection.

> **Tip:** for an IRTF Research Group, pass its shortname (e.g. `cfrg`, `hrpc`, `pearg`) wherever
> the docs use `<name>` — it works the same as a WG.


## Modes of operation

There are three supported workflows:

1. **Use it as an MCP server** — register it with Claude, Codex, Cursor, Gemini, Zed,
   etc. and ask questions across any corpus you've gathered. Two ways to run it:
   - **[Local MCP](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-local.md)** — one server
     subprocess per client, on your own machine. <-- **Best starting point**
   - **[As a shared HTTP MCP
     server](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-server.md)** — one process serving
     many concurrent clients (hosted).
3. **[Use it from the CLI](https://github.com/mnot/ietf-llm/blob/main/docs/search-cli.md)** — run
   semantic search over the cache directly with `ietf-llm-search`, no LLM client required.

See the workflow documentation linked above for installation and use instructions, or the
[full documentation listing](https://github.com/mnot/ietf-llm/blob/main/docs/README.md).

All three read a corpus you first **gather** with `ietf-llm <corpus>`.

The RFC series is the exception: `search_rfc_index`, `search_rfc_text`,
`get_rfc_info` and `get_rfc_section` cover **every published RFC** and are not
tied to a corpus. Their data arrives on its own — a gather pulls it as
housekeeping — but a machine that only ever *reads*, such as a hosted MCP
deployment, never runs a gather and so never gets it. Set that machine up in
one step:

```
ietf-llm --init
```

That refreshes the RFC metadata mirror and the effort catalog, installs the
full text of the series from the seed store (about 285 MB, which is what makes
`search_rfc_text` and `get_rfc_section` work), and syncs the norms skills. It
gathers nothing, and it is safe to re-run.

> **Heads up — gathering reaches the network by default.** To make a first gather fast, `ietf-llm`
> pulls a prebuilt snapshot of covered corpora from the public
> [seed store](https://github.com/mnot/ietf-llm/blob/main/docs/seed-store.md) at
> `seed-store.mnot.net` (and `list_corpora` looks its catalog up there), then freshens only the
> delta — skipping most of the embedding and download cost. **To turn this off:** `ietf-llm <corpus>
> --no-seed` (it *persists* across gathers; re-enable with `--seed`), or set `IETF_LLM_SEED_ENABLED=off`
> (or `IETF_LLM_SEED_URL=off`) in your environment. With seeding off, gathers run fully cold and
> offline. It is best-effort either way — an unreachable or unlisted corpus just gathers from scratch.

