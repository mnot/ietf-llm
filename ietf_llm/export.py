"""
Export a gathered WG cache to a downstream consumer.

Two sinks are supported and they are completely independent of the gather
pipeline — the cache (~/.cache/ietf-llm/<wg>/files/) is the canonical
source of truth, and both exports read from it.

  directory(wg, destination, ...)
      Mirror the public-facing text/markdown files (drafts, RFCs, minutes,
      transcripts, mailing list dumps, GitHub issue dumps, digests) into
      `destination`. Always produces a complete, fresh dump — there is no
      incremental / delta mode. Re-upload the whole directory each time
      you want NotebookLM to see changes; per project policy, the
      recommended workflow is to create a *new* NotebookLM notebook per
      update rather than try to diff into an existing one.

      The cache stores files in a directory hierarchy
      (`meetings/ietf125/minutes.md`, `threads/<slug>.md`, etc.) but
      NotebookLM expects flat filenames as separate sources. We flatten
      on the way out by joining the cache's relative path with `-`
      (e.g. `meetings/ietf125/minutes.md` → `meetings-ietf125-minutes.md`).
      That keeps the on-disk cache organised while preserving the flat
      shape NotebookLM uploads expect.

  notebooklm(wg, gcp_project, credentials_file, token_file, ...)
      Create a fresh notebook in NotebookLM Enterprise on the given GCP
      project and upload every relevant cached file as a source. Requires
      Google Workspace Enterprise with NotebookLM enabled and Discovery
      Engine API access.
"""

from __future__ import annotations

import os
import shutil
from typing import List

from .notebooklm import create_notebook, get_credentials, upload_source
from .utils import (
    LogLevel,
    Verbosity,
    get_wg_file_cache_dir,
    get_wg_title,
    log,
)


# Files we mirror / upload. JSON archives are internal and excluded;
# PDFs aren't supported by the downstream sinks (NotebookLM wants text).
_TEXT_SUFFIXES = (".txt", ".md")


def _exportable_files(cache_dir: str) -> List[tuple[str, str]]:
    """Walk the cache recursively and return (absolute path, flat name)
    tuples for every exportable file.

    The flat name is derived from the relative path under the cache by
    replacing `/` with `-`, giving NotebookLM a flat set of distinctly-
    named sources that still encode their cache location.
    """
    out: List[tuple[str, str]] = []
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            if not name.endswith(_TEXT_SUFFIXES):
                continue
            relpath = os.path.relpath(path, cache_dir)
            flat = relpath.replace(os.sep, "-").replace("/", "-")
            out.append((path, flat))
    out.sort(key=lambda kv: kv[1])
    return out


def directory(
    wg: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> int:
    """Mirror exportable cache files for `wg` into `destination`.

    Always produces a complete dump. Files already present at the
    destination with identical bytes are left alone (so re-runs are
    cheap); files present at the destination but absent from the cache
    are removed. Returns the number of files written or removed.
    """
    cache_dir = get_wg_file_cache_dir(wg)
    if not os.path.isdir(cache_dir):
        log(
            f"No cache for {wg}. Run `ietf-llm {wg}` first.",
            verbose,
            level=LogLevel.ERROR,
        )
        return 0

    os.makedirs(destination, exist_ok=True)
    sources = _exportable_files(cache_dir)
    source_names = {flat for _src, flat in sources}

    changes = 0

    # Add / update.
    for src, flat in sources:
        dst = os.path.join(destination, flat)
        if os.path.exists(dst) and _same_bytes(src, dst):
            continue
        shutil.copy2(src, dst)
        changes += 1

    # Prune anything in the destination that isn't in the cache (and
    # isn't a hidden file the user might have dropped there).
    for name in os.listdir(destination):
        if name.startswith("."):
            continue
        if name in source_names:
            continue
        if not name.endswith(_TEXT_SUFFIXES):
            continue
        os.remove(os.path.join(destination, name))
        changes += 1

    log(
        f"Exported {len(sources)} files to {destination} "
        f"({changes} added/updated/removed).",
        verbose,
        level=LogLevel.STATUS,
    )
    return changes


def _same_bytes(path_a: str, path_b: str) -> bool:
    if os.path.getsize(path_a) != os.path.getsize(path_b):
        return False
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        return fa.read() == fb.read()


def notebooklm(
    wg: str,
    gcp_project: str,
    credentials_file: str,
    token_file: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> int:
    """Create a NotebookLM notebook and upload every exportable cache file.

    Returns the number of sources successfully uploaded.
    """
    cache_dir = get_wg_file_cache_dir(wg)
    if not os.path.isdir(cache_dir):
        log(
            f"No cache for {wg}. Run `ietf-llm {wg}` first.",
            verbose,
            level=LogLevel.ERROR,
        )
        return 0

    log("Exporting to NotebookLM...", verbose, level=LogLevel.STATUS)

    creds = get_credentials(credentials_file, token_file, verbose=verbose)
    if not creds:
        log("Authentication failed.", verbose, level=LogLevel.ERROR)
        return 0

    wg_title = get_wg_title(wg)
    notebook_title = f"IETF {wg_title} Working Group"
    notebook_id = create_notebook(
        gcp_project, notebook_title, creds, verbose=verbose
    )
    if not notebook_id:
        log("Failed to create notebook.", verbose, level=LogLevel.ERROR)
        return 0

    success = 0
    for path, flat in _exportable_files(cache_dir):
        if upload_source(
            gcp_project, notebook_id, path, creds,
            display_name=flat, verbose=verbose,
        ):
            success += 1

    if success > 0:
        log(
            f"Successfully uploaded {success} files to '{notebook_title}'.",
            verbose,
            level=LogLevel.STATUS,
        )
    else:
        log("No files were uploaded.", verbose, level=LogLevel.ERROR)
    return success
