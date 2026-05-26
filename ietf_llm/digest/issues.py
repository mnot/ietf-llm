"""GitHub issues digest: one row per issue across all tracked repos.

Reads each {wg}-github-<repo>.json archive in the cache and emits a
markdown table sorted open-first, newest-first within each group.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..gather.issue_files import _detect_duplicate_of, _participants, issue_slug
from ..people import Registry
from ..utils import LogLevel, Verbosity, log
from .helpers import _state_is_open
from .summarizer import _Summarizer

_ISSUE_PROMPT = (
    "Summarize this IETF working group GitHub issue in ONE sentence "
    "(max 25 words). Focus on the substantive question or proposal, not "
    "process. No preamble.\n\nTitle: {title}\n\n{body}"
)


def _build_issues_digest(
    cache_dir: str,
    wg: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
    registry: Optional[Registry] = None,
) -> Optional[str]:
    """Build {wg}-_issues.md from cached GitHub JSON archives."""
    gh_files = sorted(
        f for f in os.listdir(cache_dir)
        if f.startswith(f"{wg}-github-") and f.endswith(".json")
    )
    if not gh_files:
        return None

    out_path = os.path.join(cache_dir, f"{wg}-_issues.md")
    total_open = 0
    total_closed = 0

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: GitHub issues digest\n\n")
        fh.write(
            "One row per issue across all tracked repos. For full discussion, "
            f"open the matching `{wg}-github-<repo>.txt` file and search for "
            "`Issue #N:`.\n\n"
        )

        for gh_file in gh_files:
            path = os.path.join(cache_dir, gh_file)
            try:
                with open(path, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except (json.JSONDecodeError, OSError) as err:
                log(f"Skipping {gh_file}: {err}", verbose, level=LogLevel.ERROR)
                continue

            repo = data.get("repo", gh_file)
            issues = data.get("issues", []) or []

            fh.write(f"## {repo}\n\n")
            fh.write(
                f"_Per-issue files: "
                f"`{wg}-issue-{repo.replace('/', '-').lower()}-*.md` "
                f"({len(issues)} issues)_\n\n"
            )

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
            issues_sorted.sort(
                key=lambda i: 0 if _state_is_open(i.get("state")) else 1
            )

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
                file_cell = f"`{wg}-issue-{issue_slug(repo, number)}.md`"
                # Surface "duplicate of #N" when detected. Empty for
                # the common case; the column header keeps the
                # affordance discoverable.
                dup_of = _detect_duplicate_of(issue)
                dup_cell = f"#{dup_of}" if dup_of is not None else ""

                if summarizer.active():
                    body = (issue.get("body") or "").strip()
                    summary = summarizer.summarize(
                        _ISSUE_PROMPT.format(title=title, body=body or "(no body)")
                    ) or ""
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

        fh.write(
            f"\n_Totals: {total_open} open, {total_closed} closed_\n"
        )

    log(
        f"Wrote issues digest: {total_open} open, {total_closed} closed",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path
