# Search from the CLI

**This document is for:** running semantic search over a gathered corpus from the command line with
`ietf-llm-search`, with no LLM client involved. Back to the [docs index](README.md).

`ietf-llm-search` prints ranked excerpts to stdout, for reading or as input to another tool.

## Installing

```bash
pipx install 'ietf-llm[local-embeddings]'
```

The on-device search model needs the `local-embeddings` extra; a [remote endpoint](models.md) works
without it. Then [gather a corpus](gathering.md) to search.

Behind a TLS-intercepting proxy, ietf-llm verifies against your OS trust store. If the proxy's root
isn't installed there, point `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` at its PEM.

## Searching

```bash
ietf-llm-search httpbis "skepticism about cookie partitioning" -k 8
```

Each hit shows its relevance score, source file, chunk index and line range, title, and a snippet.
The exit status is non-zero, and `(no results)` goes to stderr, when nothing matches.

```
ietf-llm-search [OPTIONS] <name> "<query>"
```

`<name>` is the gathered corpus and `<query>` is natural language.

| Option | Description |
|---|---|
| `-k`, `--top N` | Number of hits to return (default 10). |
| `--format FMT` | `text` (default), or `tsv` for one tab-separated row per hit (score, file, chunk index, line range, title, snippet). |
| `--file LIKE` | Only chunks whose file path matches a SQL `LIKE` pattern (`%` is the wildcard), e.g. `'%threads/%'` (mailing list), `'%issues/%'` (GitHub issues), `'%drafts/%'`. |
| `--since ISO_DATE` | Only chunks dated on or after the date (e.g. `2026-01-01`). Undated chunks, such as drafts and transcripts, are excluded. |
| `--until ISO_DATE` | Only chunks dated on or before the date. |
| `-q`, `--quiet` | Suppress status logging. |
| `--version` | Print the version and exit. |
