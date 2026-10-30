# Use with NotebookLM

**This document is for:** exporting a gathered corpus into [NotebookLM](https://notebooklm.google.com/),
either as a directory of files you upload by hand or pushed straight to a NotebookLM Enterprise
notebook. Back to the [docs index](README.md).

## Installing

A directory export needs only the base package. The NotebookLM Enterprise push also needs the
`notebooklm` extra (Google auth libraries):

```bash
pipx install ietf-llm                 # directory export
pipx install 'ietf-llm[notebooklm]'   # + Enterprise push
```

## Gathering a corpus

NotebookLM builds its own index, so pass `--no-embed` to skip the local one (this is remembered
globally; `--embed` turns the index back on):

```bash
ietf-llm httpbis --no-embed \
    --github httpwg/http-core --github httpwg/http-extensions
```

See [Gathering a corpus](gathering.md) for the rest.

Each export is a complete, fresh dump. Create a new notebook on each refresh rather than merging
updates into an existing one.

## Exporting to a local directory

```bash
ietf-llm-export httpbis --destination ~/notebooklm/httpbis
```

Drag the directory's contents into NotebookLM as sources. To stay under NotebookLM's limit of 50
sources (300 on Plus), per-thread mailing list conversations are bundled by year and per-issue
GitHub records by repo. `--no-bundle` writes one file per thread and issue instead.

`ietf-llm-export` exits 1, touching nothing, for a corpus that has not been gathered. A gathered
corpus with nothing exportable exits 0 after saying so.

## Exporting to NotebookLM Enterprise

With Google Workspace Enterprise and NotebookLM enabled, `ietf-llm-export` can create the notebook
and upload the sources directly. This needs the `notebooklm` extra:

```bash
ietf-llm-export httpbis --create my-gcp-project-id
```

One-time setup:

1. Create a **Google Cloud project** with the **Discovery Engine API** enabled.
2. Create an "OAuth 2.0 Client ID" (Desktop App) in the
   [Cloud Console](https://console.cloud.google.com/apis/credentials).
3. Save the JSON as `client_secrets.json` in `~/.config/ietf-llm/`, or pass
   `--credentials-file PATH`.

The first run opens a browser to authorise, and caches the token at `~/.config/ietf-llm/token.json`
(`--token-file PATH` moves it).

Per-corpus export settings are saved in `~/.config/ietf-llm/<name>/export.json`, so later runs of the
same mode need only `ietf-llm-export <name>`. Switching between `--destination` and `--create`
requires `ietf-llm <name> --clear-config` first.
