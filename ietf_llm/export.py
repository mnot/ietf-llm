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

  notebooklm(wg, gcp_project, credentials_file, token_file, ...)
      Create a fresh notebook in NotebookLM Enterprise on the given GCP
      project and upload every relevant cached file as a source. Requires
      Google Workspace Enterprise with NotebookLM enabled and Discovery
      Engine API access.

Per-file vs bundled output:

The cache stores 200-1500+ small files per WG (one per thread, one
per issue, etc.) — that maximises per-tool granularity inside the
MCP server, but NotebookLM's source limits are tight (50 sources
free, 300 Plus). Active WGs blow through the free tier and brush up
against Plus.

So `directory()` and `notebooklm()` default to BUNDLED output: per-
year thread bundles (mirroring the old `mail-archive-YYYY.txt`
pattern) and per-repo issue bundles. Drafts, RFCs, meeting artefacts,
and digests stay one-per-file because each is a substantial standalone
document worth citing individually. Pass `bundle=False` for the full
granular dump.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Dict, List, Tuple

from .atomicio import write_if_changed
from .datatracker_api import get_wg_title
from .paths import get_wg_file_cache_dir
from .utils import LogLevel, Verbosity, log

# Files we mirror / upload. JSON archives are internal and excluded;
# PDFs aren't supported by the downstream sinks (NotebookLM wants text).
_TEXT_SUFFIXES = (".txt", ".md")

#: Filename date prefix on per-thread files, e.g. `2026-05-15-foo.md`.
_THREAD_YEAR_RE = re.compile(r"^(\d{4})-\d{2}-\d{2}-")


def _exportable_files(
    cache_dir: str,
    bundle: bool = True,
) -> List[Tuple[str, str, str]]:
    """Walk the cache and return `(content, flat_name, kind)` for every
    exportable source.

    `content` is the literal bytes (string) to write/upload. `flat_name`
    is the destination filename (cache dir is implied by the parent).
    `kind` is a short tag ("thread-bundle", "issue-bundle", "drafts",
    "meetings", "digests", "raw", "charter") for logging / diagnostics.

    When `bundle=True` (the default), per-thread .md files collapse into
    yearly bundles (one source per year) and per-issue .md files
    collapse into per-repo bundles (one source per repo). Drafts,
    meeting artefacts, digests, and raw bulk files stay one-per-file.
    """
    out: List[Tuple[str, str, str]] = []
    if not os.path.isdir(cache_dir):
        return out

    thread_files: List[str] = []
    issue_files_by_repo: Dict[str, List[str]] = {}
    passthrough: List[Tuple[str, str, str]] = []

    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            if not name.endswith(_TEXT_SUFFIXES):
                continue
            relpath = os.path.relpath(path, cache_dir)
            kind = _classify_relpath(relpath)
            if bundle and kind == "threads":
                thread_files.append(path)
                continue
            if bundle and kind == "issues":
                # The repo slug is the directory level under issues/.
                # `issues/<repo>/<N>.md` → repo = the path component.
                parts = relpath.split("/")
                if len(parts) >= 3:
                    repo_slug = parts[1]
                    issue_files_by_repo.setdefault(repo_slug, []).append(path)
                continue
            # Passthrough: read the file's content for direct copy /
            # upload. Each one becomes its own source.
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            flat = relpath.replace(os.sep, "-").replace("/", "-")
            passthrough.append((content, flat, kind))

    out.extend(passthrough)

    if bundle and thread_files:
        for year, content in _bundle_threads(thread_files).items():
            out.append((content, f"threads-{year}.md", "thread-bundle"))

    if bundle and issue_files_by_repo:
        for repo_slug, paths in issue_files_by_repo.items():
            content = _bundle_issues(paths)
            out.append((content, f"issues-{repo_slug}.md", "issue-bundle"))

    out.sort(key=lambda kv: kv[1])
    return out


_RELPATH_KINDS = ("threads", "issues", "drafts", "meetings", "digests", "raw")


def _classify_relpath(relpath: str) -> str:
    """Coarse kind tag based on the cache layout."""
    for kind in _RELPATH_KINDS:
        if relpath.startswith(f"{kind}/"):
            return kind
    return "other"


def _bundle_threads(paths: List[str]) -> Dict[str, str]:
    """Concatenate per-thread files into one bundle per year.

    The year is read from the filename (per-thread files are named
    `YYYY-MM-DD-<slug>.md`). Threads without a parseable date go into
    a `_undated` bucket so they aren't silently dropped.
    """
    by_year: Dict[str, List[str]] = {}
    for path in sorted(paths):
        match = _THREAD_YEAR_RE.match(os.path.basename(path))
        year = match.group(1) if match else "_undated"
        by_year.setdefault(year, []).append(path)
    bundles: Dict[str, str] = {}
    for year, year_paths in by_year.items():
        parts: List[str] = [f"# Mailing list threads — {year}\n\n"]
        parts.append(
            f"_{len(year_paths)} thread(s), reconstructed via "
            "In-Reply-To / References headers. Each section is one "
            "thread, separated by `---`._\n"
        )
        for thread_path in year_paths:
            try:
                with open(thread_path, "r", encoding="utf-8", errors="replace") as fh:
                    parts.append("\n\n---\n\n")
                    parts.append(fh.read())
            except OSError:
                continue
        bundles[year] = "".join(parts)
    return bundles


def _bundle_issues(paths: List[str]) -> str:
    """Concatenate per-issue files into one repo bundle.

    Issues are sorted by their numeric identifier (read from the
    filename's `<N>.md` stem), so the bundle reads in issue-number
    order rather than alphabetic-string order (which would put #100
    before #2).
    """

    def _num(path: str) -> int:
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            return int(stem)
        except ValueError:
            return -1

    sorted_paths = sorted(paths, key=_num)
    parts: List[str] = [
        f"# GitHub issues ({len(sorted_paths)} issues)\n\n",
        "_One section per issue, separated by `---`. Sorted by issue number._\n",
    ]
    for issue_path in sorted_paths:
        try:
            with open(issue_path, "r", encoding="utf-8", errors="replace") as fh:
                parts.append("\n\n---\n\n")
                parts.append(fh.read())
        except OSError:
            continue
    return "".join(parts)


def directory(
    wg: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
    bundle: bool = True,
) -> int:
    """Mirror exportable cache files for `wg` into `destination`.

    Always produces a complete dump. Files already present at the
    destination with identical bytes are left alone (so re-runs are
    cheap); files present at the destination but absent from the cache
    are removed. Returns the number of files written or removed.

    `bundle=True` (default) collapses per-thread files into yearly
    bundles and per-issue files into per-repo bundles, dramatically
    reducing the source count for NotebookLM. Pass `bundle=False` for
    the fully granular dump.
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
    sources = _exportable_files(cache_dir, bundle=bundle)
    source_names = {flat for _content, flat, _kind in sources}

    changes = 0

    # Add / update. We compare bytes against the destination file when
    # one already exists so re-runs that produce identical content are
    # free (no copy, no mtime churn).
    for content, flat, _kind in sources:
        dst = os.path.join(destination, flat)
        # write_if_changed skips an unchanged re-export (no mtime churn) and
        # writes atomically, so a crash mid-export can't truncate a file.
        if write_if_changed(dst, content):
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
        f"({'bundled' if bundle else 'per-file'}; "
        f"{changes} added/updated/removed).",
        verbose,
        level=LogLevel.STATUS,
    )
    return changes


def notebooklm(
    wg: str,
    gcp_project: str,
    credentials_file: str,
    token_file: str,
    verbose: Verbosity = Verbosity.STATUS,
    bundle: bool = True,
) -> int:
    """Create a NotebookLM notebook and upload every exportable cache file.

    Returns the number of sources successfully uploaded. `bundle=True`
    (default) keeps the source count well under NotebookLM's per-
    notebook limits (50 free / 300 Plus); see `directory()` for the
    bundling shape.
    """
    # Google auth libraries live behind the optional `notebooklm` extra,
    # so this push path imports them lazily and fails cleanly when the
    # extra isn't installed (mirror-mode export needs none of this).
    try:
        # pylint: disable=import-outside-toplevel
        from .notebooklm import create_notebook, get_credentials, upload_source
    except ImportError:
        log(
            "NotebookLM export needs the optional `notebooklm` extra "
            "(Google auth libraries):\n"
            "  pipx install 'ietf-llm[notebooklm]'\n"
            "  # or with pip: pip install 'ietf-llm[notebooklm]'",
            verbose,
            level=LogLevel.ERROR,
        )
        return 0

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
    notebook_id = create_notebook(gcp_project, notebook_title, creds, verbose=verbose)
    if not notebook_id:
        log("Failed to create notebook.", verbose, level=LogLevel.ERROR)
        return 0

    sources = _exportable_files(cache_dir, bundle=bundle)
    # Stage bundle contents to temp files so upload_source's existing
    # file-based path keeps working without growing a bytes-uploading
    # cousin. Each upload reads from the staged file, then we clean up.
    success = 0
    with tempfile.TemporaryDirectory(prefix="ietf-llm-export-") as staging:
        for content, flat, _kind in sources:
            staged = os.path.join(staging, flat)
            with open(staged, "w", encoding="utf-8") as fh:
                fh.write(content)
            if upload_source(
                gcp_project,
                notebook_id,
                staged,
                creds,
                display_name=flat,
                verbose=verbose,
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
