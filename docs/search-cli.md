# Search from the CLI

**This document is for:** running semantic search over a gathered corpus from the command line with
`ietf-llm-search`, with no LLM client involved. — Back to the [docs index](README.md).

`ietf-llm-search` runs semantic search over a gathered corpus and prints ranked excerpts to stdout —
useful on its own, or as input to another tool.

## Installing

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The on-device search model needs the `local-embeddings` extra. Alternatively, point at a
[remote endpoint](models.md). Then [gather a corpus](gathering.md) to search.

**Behind a corporate firewall** with TLS interception? If you encounter errors, you may need the
`certs` extra:

```bash
pipx uninstall ietf-llm
pipx install 'ietf-llm[local-embeddings,certs]'
```

## Searching

```bash
ietf-llm-search httpbis "skepticism about cookie partitioning" -k 8
```

Each hit prints its relevance score, source file, chunk index and line range, title, and a snippet:

```
[1] score=0.812  threads/2024-03-12-partition-by-top-level-site.md  (chunk 3, L42-61)
    [7] 2024-03-12 14:22 — Alice Chen (Chair)
    Still skeptical that partitioning by top-level site alone closes the cross-site
    tracking vector — a determined tracker can re-link via login state.

[2] score=0.764  issues/httpwg-http-extensions/1289.md  (chunk 0, L1-20)
    #1289: Reconsider double-keying vs. full partitioning
    Pushback on the cost/benefit of full partitioning; double-keying may suffice for
    the threat model we actually care about here.
```

Exit status is non-zero (and `(no results)` goes to stderr) when nothing matches.

### Options

```
ietf-llm-search [OPTIONS] <name> "<query>"
```

`<name>` is the gathered corpus (a WG short name such as `httpbis`, or any other corpus name);
`<query>` is natural language.

| Option | Description |
|---|---|
| `-k`, `--top N` | Number of hits to return (default: 10). |
| `--format FMT` | Output format: `text` (default, human-readable) or `tsv` — one tab-separated row per hit (score, file, chunk index, line range, title, snippet), for piping into other tools. |
| `--file LIKE` | Restrict to chunks whose file path matches a SQL `LIKE` pattern (`%` is the wildcard). E.g. `'%threads/%'` (mailing-list threads), `'%issues/%'` (GitHub issues), `'%drafts/%'`. |
| `--since ISO_DATE` | Only chunks dated on or after `ISO_DATE` (e.g. `2026-01-01`). Applies to mailing-list and issue chunks; undated draft / transcript chunks are excluded. |
| `--until ISO_DATE` | Only chunks dated on or before `ISO_DATE`. |
| `-q`, `--quiet` | Suppress status logging. |
| `--version` | Print the version and exit. |
