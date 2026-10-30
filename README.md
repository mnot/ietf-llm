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

## Quick start

1. Install with [pipx](https://pipx.pypa.io/stable/):

   ```bash
   pipx install 'ietf-llm[local-embeddings]'
   ```

2. Gather a group (here `httpbis`; an IRTF Research Group such as `cfrg` works the same way):

   ```bash
   ietf-llm httpbis
   ```

3. Register the MCP server with your client — for Claude Code:

   ```bash
   claude mcp add ietf-llm -- ietf-llm-mcp
   ```

   Other clients are covered in [Running the MCP server locally](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-local.md).

4. Ask your client about the group.

## What you can ask

The RFC, chartered-group and norms tools work without a gather; the rest read a corpus you've
gathered. `draft_status`, `review_record` and `meeting_schedule` also look up current state on the
Datatracker.

- **Orientation** — what's gathered, what a group is doing, who's active, what's open.
- **Chartered groups and RFCs** — which group or RFC covers a topic; charters, milestones, history,
  and RFC text across the whole series.
- **Discussion** — what was argued about a topic, replies to a message, chair calls and polls, and
  whether something was *ever* said or proposed.
- **Issues, pull requests and drafts** — filtered catalogues, verbatim draft text by section, and
  why the text says what it does.
- **Meetings** — minutes with poll tallies, attendance, the upcoming schedule.
- **Norms** — how to read the record without overclaiming consensus, and how to contribute without
  wasting the group's time.

## Ways to run it

- **[Local MCP server](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-local.md)** — one server per client, on your own machine. Best
  starting point.
- **[Shared HTTP MCP server](https://github.com/mnot/ietf-llm/blob/main/docs/mcp-server.md)** — one hosted process serving many clients.
- **[CLI search](https://github.com/mnot/ietf-llm/blob/main/docs/search-cli.md)** — semantic search over the cache with `ietf-llm-search`, no
  LLM client needed.
- **[NotebookLM export](https://github.com/mnot/ietf-llm/blob/main/docs/notebooklm.md)** — mirror a corpus to files, or push it to NotebookLM
  Enterprise.

Every mode reads a corpus you first [gather](https://github.com/mnot/ietf-llm/blob/main/docs/gathering.md) with `ietf-llm <corpus>`. The full
documentation listing is in [docs/README.md](docs/README.md).

> **Gathering reaches the network.** To make a first gather fast, `ietf-llm` pulls a prebuilt
> snapshot of covered corpora from the public [seed store](https://github.com/mnot/ietf-llm/blob/main/docs/seed-store.md) at
> `seed-store.mnot.net`, then freshens only the delta; `list_corpora` looks its catalog up there too.
> To turn this off, run `ietf-llm <corpus> --no-seed` (it persists across gathers; `--seed`
> re-enables it), or set `IETF_LLM_SEED_ENABLED=off` or `IETF_LLM_SEED_URL=off`. With seeding off,
> gathers run fully cold. An unreachable or unlisted corpus just gathers from scratch.
