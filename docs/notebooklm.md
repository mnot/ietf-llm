# Use with NotebookLM

**This document is for:** exporting a gathered corpus into
[NotebookLM](https://notebooklm.google.com/), either as a directory of files you upload by hand or
pushed straight to a NotebookLM Enterprise notebook. — Back to the [docs index](README.md).

NotebookLM ingests a corpus as a set of source files. `ietf-llm-export` turns the gathered cache
into an upload-ready directory, or pushes straight to a NotebookLM Enterprise notebook using its
API.

## Installing

A directory export needs only the base package; the NotebookLM Enterprise push additionally needs
the `notebooklm` extra (Google-auth libraries):

```bash
pipx install ietf-llm                 # directory export
pipx install 'ietf-llm[notebooklm]'   # + Enterprise push
```

## Gathering a corpus

NotebookLM has its own index, so the local semantic-search index is wasted work for this workflow —
pass `--no-embed` to skip building it:

```bash
ietf-llm httpbis --no-embed \
    --github httpwg/http-core --github httpwg/http-extensions
```

For more information, see [gather a corpus](gathering.md).

> **Workflow note:** export always produces a complete fresh dump. Create a new notebook on each
> refresh rather than trying to merge updates into an existing one.


## Exporting to a local directory

```bash
ietf-llm-export httpbis --destination ~/notebooklm/httpbis
```

Drag the directory's contents into NotebookLM as sources. Per-thread mailing list conversations and
per-issue GitHub records are bundled by year / repo to stay under NotebookLM's 50-source free /
300-source Plus limit.

## Exporting to NotebookLM Enterprise

If you have Google Workspace Enterprise with NotebookLM enabled, `ietf-llm-export` can create the
notebook and upload sources directly. The push path needs the `notebooklm` extra (see
[Installing](#installing)):

```bash
ietf-llm-export httpbis --create my-gcp-project-id
```

One-time setup:

1. **Google Cloud Project** with the **Discovery Engine API** enabled.
2. **OAuth credentials**: create an "OAuth 2.0 Client ID" (Desktop App) in the
   [Cloud Console](https://console.cloud.google.com/apis/credentials).
3. **Save the JSON** as `client_secrets.json` in `~/.config/ietf-llm/` (or pass
   `--credentials-file PATH`).

First run opens a browser to authorise; the token is cached at `~/.config/ietf-llm/token.json`.

Per-corpus export settings are persisted at `~/.config/ietf-llm/<name>/export.json` — subsequent
runs of the same mode need only `ietf-llm-export <name>`.
