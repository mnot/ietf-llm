# ietf-llm

Maintain a local, queryable corpus of an [IETF](https://www.ietf.org/) Working Group's public
record — charter, drafts, RFCs, meeting agendas, minutes, slides, transcripts, mailing list
archives, and GitHub issues — for use with LLM-based tools.

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

> ***Not just WGs***: ietf-llm also works with [IRTF](https://irtf.org/) Research Groups. Pass the
> RG's shortname (e.g. `cfrg`, `hrpc`, `pearg`) wherever the docs use `<name>`.


## Modes of operation

There are three supported workflows:

1. **Use it as an MCP server** — register it with Claude, Codex, Cursor, Gemini, Zed,
   etc. and ask questions across any corpus you've gathered. Two ways to run it:
   - **[Local MCP](docs/mcp-local.md)** — one server subprocess per client, on your own machine.
   - **[As a shared HTTP MCP server](docs/mcp-server.md)** — one process serving many concurrent
     clients (hosted).
2. **[Use it with NotebookLM](docs/notebooklm.md)** — export the gathered corpus as a directory of
   clean text files and ingest it as a notebook (or push directly to NotebookLM Enterprise).
3. **[Use it from the CLI](docs/search-cli.md)** — run semantic search over the cache directly with
   `ietf-llm-search`, no LLM client required.

See the workflow documentation linked above for installation and use instructions, or the
[full documentation listing](docs/README.md).

