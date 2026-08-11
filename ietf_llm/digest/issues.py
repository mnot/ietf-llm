"""GitHub issues digest: one row per issue across all tracked repos.

Reads each {wg}-github-<repo>.json archive in the cache and emits a
markdown table sorted open-first, newest-first within each group.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, TextIO

from ..atomicio import atomic_open
from ..gather.sources.issue_files import _detect_duplicate_of, _participants
from ..log import LogLevel, Verbosity, log
from ..paths import digest_path, github_dir, issue_path, remove_stale_digest
from ..people import Registry
from .helpers import _state_is_open
from .summarizer import _Summarizer

_ISSUE_PROMPT = (
    "Summarize this IETF working group GitHub issue in ONE sentence "
    "(max 25 words). Focus on the substantive question or proposal, not "
    "process. No preamble.\n\nTitle: {title}\n\n{body}"
)


def _write_label_glossary(fh: TextIO, data: Dict[str, Any], issues: List[Any]) -> None:
    """Write the repo's label vocabulary, with descriptions and use counts.

    The archive's repo-level `labels` array is the only place a label's
    *meaning* is recorded — issues carry bare names. Without this, a reader
    meeting `ready to close` or `has-consensus` for the first time has to
    infer it from the issues that carry it.

    Deliberately a bullet list, not a table: `read_digest` parses this file
    into tables and applies the issue filters to every one it finds, so a
    glossary table would be silently emptied by `state="open"` and would
    have to be special-cased in the query layer. Labels defined but never
    used are still listed — an unused label is a statement about how the
    repo means to organise itself.
    """
    labels = [lbl for lbl in (data.get("labels") or []) if isinstance(lbl, dict)]
    if not labels:
        return
    counts: Dict[str, int] = {}
    for issue in issues:
        for name in issue.get("labels") or []:
            if isinstance(name, str):
                counts[name] = counts.get(name, 0) + 1
    fh.write(f"**Label vocabulary** ({len(labels)} defined):\n\n")
    for label in sorted(labels, key=lambda l: str(l.get("name") or "")):
        name = label.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = label.get("description")
        gloss = description.strip() if isinstance(description, str) else ""
        used = counts.get(name, 0)
        suffix = f" ({used} issue{'' if used == 1 else 's'})" if used else " (unused)"
        fh.write(f"- `{name}`{' — ' + gloss if gloss else ''}{suffix}\n")
    fh.write("\n")


def _build_issues_digest(  # pylint: disable=too-many-locals
    cache_dir: str,
    wg: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
    registry: Optional[Registry] = None,
) -> Optional[str]:
    """Build `digests/issues.md` from cached GitHub JSON archives."""
    archives_dir = github_dir(cache_dir)
    if not os.path.isdir(archives_dir):
        remove_stale_digest(cache_dir, "issues")
        return None
    gh_files = sorted(f for f in os.listdir(archives_dir) if f.endswith(".json"))
    if not gh_files:
        remove_stale_digest(cache_dir, "issues")
        return None

    out_path = digest_path(cache_dir, "issues")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    total_open = 0
    total_closed = 0

    with atomic_open(out_path) as fh:
        fh.write(f"# {wg}: GitHub issues digest\n\n")
        fh.write(
            "One row per issue across all tracked repos. For full discussion, "
            "open the matching per-issue file (see the File column).\n\n"
        )

        for gh_file in gh_files:
            path = os.path.join(archives_dir, gh_file)
            try:
                with open(path, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except (json.JSONDecodeError, OSError) as err:
                log(f"Skipping {gh_file}: {err}", verbose, level=LogLevel.ERROR)
                continue

            repo = data.get("repo", gh_file)
            issues = data.get("issues", []) or []
            repo_slug = repo.replace("/", "-").lower()

            fh.write(f"## {repo}\n\n")
            fh.write(
                f"_Per-issue files: `issues/{repo_slug}/*.md` "
                f"({len(issues)} issues)_\n\n"
            )
            _write_label_glossary(fh, data, issues)

            if summarizer.active():
                fh.write(
                    "| # | State | Title | Labels | Comments | Updated | "
                    "Author | Participants | Dup-of | File | Summary |\n"
                    "|---|-------|-------|--------|----------|---------|"
                    "--------|--------------|--------|------|---------|\n"
                )
            else:
                fh.write(
                    "| # | State | Title | Labels | Comments | Updated | "
                    "Author | Participants | Dup-of | File |\n"
                    "|---|-------|-------|--------|----------|---------|"
                    "--------|--------------|--------|------|\n"
                )

            # Sort: open first, then by updated desc within each group.
            # All state comparisons go through _state_is_open(), which
            # tolerates both 'open'/'closed' (REST) and 'OPEN'/'CLOSED'
            # (GraphQL) forms. Two passes leveraging Python's stable sort:
            # first by date as the secondary key, then by state as primary.
            issues_sorted = sorted(
                issues,
                key=lambda i: i.get("updatedAt") or i.get("createdAt") or "",
                reverse=True,
            )
            issues_sorted.sort(key=lambda i: 0 if _state_is_open(i.get("state")) else 1)

            for issue in issues_sorted:
                number = issue.get("number", "?")
                title = (issue.get("title") or "(no title)").replace("|", "\\|")
                state = issue.get("state", "?")
                labels = ", ".join(issue.get("labels", []) or [])
                labels = labels.replace("|", "\\|")
                n_comments = len(issue.get("comments", []) or [])
                updated = (issue.get("updatedAt") or issue.get("createdAt") or "")[:10]
                raw_author = issue.get("author") or "?"
                if registry is not None:
                    raw_author = registry.canonical_for_github(raw_author) or raw_author
                author = raw_author.replace("|", "\\|")

                if _state_is_open(state):
                    total_open += 1
                else:
                    total_closed += 1

                participants = _participants(issue, registry)
                # Drop the author since it's already in its own column.
                others = [p for p in participants if p != raw_author]
                participants_cell = ", ".join(others).replace("|", "\\|")
                # Relative path is what the chunker and MCP tools speak.
                relpath = os.path.relpath(
                    issue_path(cache_dir, repo, number),
                    cache_dir,
                )
                file_cell = f"`{relpath}`"
                # Surface "duplicate of #N" when detected. Empty for
                # the common case; the column header keeps the
                # affordance discoverable.
                dup_of = _detect_duplicate_of(issue)
                dup_cell = f"#{dup_of}" if dup_of is not None else ""

                if summarizer.active():
                    body = (issue.get("body") or "").strip()
                    summary = (
                        summarizer.summarize(
                            _ISSUE_PROMPT.format(title=title, body=body or "(no body)")
                        )
                        or ""
                    )
                    summary = summary.replace("|", "\\|")
                    fh.write(
                        f"| {number} | {state} | {title} | {labels} | "
                        f"{n_comments} | {updated} | {author} | "
                        f"{participants_cell} | {dup_cell} | "
                        f"{file_cell} | {summary} |\n"
                    )
                else:
                    fh.write(
                        f"| {number} | {state} | {title} | {labels} | "
                        f"{n_comments} | {updated} | {author} | "
                        f"{participants_cell} | {dup_cell} | "
                        f"{file_cell} |\n"
                    )
            fh.write("\n")

        fh.write(f"\n_Totals: {total_open} open, {total_closed} closed_\n")

    log(
        f"Wrote issues digest: {total_open} open, {total_closed} closed",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path
