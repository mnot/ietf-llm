"""Machinery shared by the per-issue and per-PR file writers.

Issues and pull requests come out of the same `github/<repo>.json`
archive and are written to disk the same way — one Markdown file per
record under `<tree>/<repo-slug>/<N>.md`, write-if-changed, with an
orphan sweep so a record that leaves the archive loses its file. Only
the rendering differs, so that is all `issue_files` / `pull_files`
implement; the walk lives here.

Kept deliberately small: this is the *how we write them* layer, not a
home for issue or PR semantics.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from ...atomicio import write_if_changed
from ...log import LogLevel, Verbosity, log
from .github_schema import archive_slug, load_archive


def last_comment_quote(
    comments: List[Dict[str, Any]],
    format_when: Callable[[Any], str],
    format_author: Callable[[str], str],
) -> Optional[str]:
    """The final comment rendered as an attributed Markdown blockquote.

    Both record kinds use this for their closing note: on an issue the
    last comment is usually the resolution ("agreed, closing"); on a PR
    closed without merging it is usually the reason it was dropped.
    Returns None when there are no comments or the last one is empty.

    Truncated hard — this is metadata, not the primary content; the full
    comment is still in the file's own section.
    """
    if not comments:
        return None
    last = comments[-1]
    body = (last.get("body") or "").strip()
    if not body:
        return None
    when = format_when(last.get("createdAt"))
    author = format_author(last.get("author") or "")
    snippet = body if len(body) <= 400 else body[:397] + "..."
    quoted = "\n".join(f"> {line}" for line in snippet.splitlines())
    return f"_by {author} on {when}:_\n\n{quoted}"


def write_record_files(
    archives_dir: str,
    out_root: str,
    records_key: str,
    repo_dir_for: Callable[[str], str],
    path_for: Callable[[str, Any], str],
    render: Callable[[str, Dict[str, Any]], str],
    noun: str,
    verbose: Verbosity,
) -> List[str]:
    """Write one Markdown file per record across every cached archive.

    `records_key` selects the array (`issues` / `pulls`); `repo_dir_for`
    and `path_for` map a repo (and number) to their destination;
    `render` turns `(repo, record)` into the file's bytes.

    Write-if-changed rather than wipe-and-rewrite: a byte-identical
    re-render leaves the file untouched, avoiding needless I/O and mtime
    churn (the embedder keys its skip on content hash anyway). Files
    under `out_root` that no archive accounts for are then removed, so a
    record deleted upstream doesn't linger.

    Returns the absolute paths of every current file.
    """
    all_paths: List[str] = []
    changed: List[str] = []
    expected: set[str] = set()
    root = os.path.realpath(out_root) + os.sep
    for name in sorted(os.listdir(archives_dir)):
        if not name.endswith(".json"):
            continue
        data = load_archive(os.path.join(archives_dir, name), verbose)
        if data is None or not data.get(records_key):
            continue
        # Paths come from the file name and display from the archive: they
        # differ for a renamed repo, and a file named `..json` makes the slug
        # `.`, so the destination must be checked rather than assumed.
        slug = archive_slug(name)
        if not os.path.realpath(repo_dir_for(slug)).startswith(root):
            log(f"Skipping {name}: outside {out_root}", verbose, level=LogLevel.ERROR)
            continue
        os.makedirs(repo_dir_for(slug), exist_ok=True)
        for record in data[records_key]:
            path = path_for(slug, record["number"])
            expected.add(os.path.relpath(path, out_root))
            all_paths.append(path)
            if write_if_changed(path, render(data["repo"], record)):
                changed.append(path)

    removed = _sweep_orphans(out_root, expected)
    if all_paths or removed:
        log(
            f"Per-{noun} files: {len(all_paths)} current "
            f"({len(changed)} written / changed, {removed} removed)",
            verbose,
            level=LogLevel.STATUS,
        )
    return all_paths


def _sweep_orphans(out_root: str, expected: "set[str]") -> int:
    """Delete `<out_root>/<repo>/<N>.md` files not in `expected` (paths
    relative to `out_root`). Returns how many went."""
    if not os.path.isdir(out_root):
        return 0
    removed = 0
    for repo_subdir in os.listdir(out_root):
        sub_path = os.path.join(out_root, repo_subdir)
        # A link could point anywhere; only real directories are ours to sweep.
        if os.path.islink(sub_path) or not os.path.isdir(sub_path):
            continue
        for name in os.listdir(sub_path):
            if not name.endswith(".md"):
                continue
            if os.path.join(repo_subdir, name) in expected:
                continue
            try:
                os.remove(os.path.join(sub_path, name))
                removed += 1
            except OSError:
                pass
    return removed
