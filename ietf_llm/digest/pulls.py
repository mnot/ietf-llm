"""GitHub pull-requests digest: one row per PR across all tracked repos.

Reads the same `github/<repo>.json` archives as the issues digest and
emits a markdown table sorted open-first, newest-first within each group.

The column that isn't in the issues digest is **Commit** — the merge
commit's abbreviated oid. That makes this table the index for the walk a
reviewer actually wants: `git blame` a line of the draft → commit → the PR
that introduced it → the issue that PR closed. Doing that by scanning a
thousand per-PR files would be silly; one grep here answers it.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from ..atomicio import atomic_open
from ..gather.sources.pull_files import _closes
from ..log import LogLevel, Verbosity, log
from ..paths import digest_path, github_dir, pull_path, remove_stale_digest
from ..people import Registry
from .helpers import _state_is_open
from .summarizer import _Summarizer

_PULL_PROMPT = (
    "Summarize this IETF working group GitHub pull request in ONE sentence "
    "(max 25 words). Focus on what the change does to the document, not "
    "process. No preamble.\n\nTitle: {title}\n\n{body}"
)

#: How many characters of the merge commit oid to show. Seven is git's
#: own abbreviation default and stays unambiguous at repo scale.
_SHORT_OID = 7


def _merge_oid(pull: Dict[str, Any]) -> str:
    commit = pull.get("mergeCommit") or {}
    if not isinstance(commit, dict):
        return ""
    oid = commit.get("oid")
    return oid[:_SHORT_OID] if isinstance(oid, str) else ""


def _build_pulls_digest(  # pylint: disable=too-many-locals
    cache_dir: str,
    wg: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
    registry: Optional[Registry] = None,
) -> Optional[str]:
    """Build `digests/pulls.md` from cached GitHub JSON archives.

    Returns None (removing any stale digest) when no archive carries a
    `pulls` array — the REST-fallback archives don't, so a corpus whose
    repos have no gh-pages `archive.json` gets no PR digest at all rather
    than an empty table.
    """
    archives_dir = github_dir(cache_dir)
    if not os.path.isdir(archives_dir):
        remove_stale_digest(cache_dir, "pulls")
        return None
    gh_files = sorted(f for f in os.listdir(archives_dir) if f.endswith(".json"))

    loaded: List[Tuple[str, List[Dict[str, Any]]]] = []
    for gh_file in gh_files:
        path = os.path.join(archives_dir, gh_file)
        try:
            with open(path, "r", encoding="utf-8") as jf:
                data = json.load(jf)
        except (json.JSONDecodeError, OSError) as err:
            log(f"Skipping {gh_file}: {err}", verbose, level=LogLevel.ERROR)
            continue
        pulls = [p for p in (data.get("pulls") or []) if isinstance(p, dict)]
        if pulls:
            loaded.append((data.get("repo", gh_file), pulls))
    if not loaded:
        remove_stale_digest(cache_dir, "pulls")
        return None

    out_path = digest_path(cache_dir, "pulls")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    total_open = 0
    total_merged = 0
    total_closed = 0
    # A state we don't recognise (or a record with none at all) is counted
    # apart rather than swept into "closed without merging" — the totals
    # line is read as a summary of outcomes, and quietly filing an unknown
    # under the most consequential-looking bucket misreports it.
    total_unknown = 0

    with atomic_open(out_path) as fh:
        fh.write(f"# {wg}: GitHub pull requests digest\n\n")
        fh.write(
            "One row per pull request across all tracked repos. The PR is "
            "where the reasoning behind a change to the document lives — the "
            "issue says what was wrong, the PR says what was done about it. "
            "For the full discussion and review, open the matching per-PR "
            "file (see the File column). `Commit` is the merge commit, so a "
            "line of text can be traced back through `git blame` to the PR "
            "that introduced it and the issue it closed.\n\n"
        )

        for repo, pulls in loaded:
            repo_slug = repo.replace("/", "-").lower()
            fh.write(f"## {repo}\n\n")
            fh.write(f"_Per-PR files: `pulls/{repo_slug}/*.md` ({len(pulls)} PRs)_\n\n")

            if summarizer.active():
                fh.write(
                    "| # | State | Title | Labels | Comments | Reviews | "
                    "Updated | Author | Merged by | Commit | Closes | File | "
                    "Summary |\n"
                    "|---|-------|-------|--------|----------|---------|"
                    "---------|--------|-----------|--------|--------|------|"
                    "---------|\n"
                )
            else:
                fh.write(
                    "| # | State | Title | Labels | Comments | Reviews | "
                    "Updated | Author | Merged by | Commit | Closes | File |\n"
                    "|---|-------|-------|--------|----------|---------|"
                    "---------|--------|-----------|--------|--------|------|\n"
                )

            # Open first, then most-recently-updated within each group —
            # same two-pass stable sort as the issues digest.
            pulls_sorted = sorted(
                pulls,
                key=lambda p: p.get("updatedAt") or p.get("createdAt") or "",
                reverse=True,
            )
            pulls_sorted.sort(key=lambda p: 0 if _state_is_open(p.get("state")) else 1)

            for pull in pulls_sorted:
                number = pull.get("number", "?")
                title = (pull.get("title") or "(no title)").replace("|", "\\|")
                state = (pull.get("state") or "?").upper()
                labels = ", ".join(
                    lbl for lbl in (pull.get("labels") or []) if isinstance(lbl, str)
                ).replace("|", "\\|")
                n_comments = len(pull.get("comments") or [])
                n_reviews = len(pull.get("reviews") or [])
                updated = (pull.get("updatedAt") or pull.get("createdAt") or "")[:10]
                raw_author = pull.get("author") or "?"
                if registry is not None:
                    raw_author = registry.canonical_for_github(raw_author) or raw_author
                author = raw_author.replace("|", "\\|")
                merged_by = pull.get("mergedBy") or ""
                if merged_by and registry is not None:
                    merged_by = registry.canonical_for_github(merged_by) or merged_by
                merged_by = merged_by.replace("|", "\\|")

                if _state_is_open(state):
                    total_open += 1
                elif state == "MERGED":
                    total_merged += 1
                elif state == "CLOSED":
                    total_closed += 1
                else:
                    total_unknown += 1

                closes = ", ".join(f"#{n}" for n in _closes(pull, repo))
                relpath = os.path.relpath(pull_path(cache_dir, repo, number), cache_dir)
                row = (
                    f"| {number} | {state} | {title} | {labels} | {n_comments} | "
                    f"{n_reviews} | {updated} | {author} | {merged_by} | "
                    f"{_merge_oid(pull)} | {closes} | `{relpath}` |"
                )
                if summarizer.active():
                    body = (pull.get("body") or "").strip()
                    summary = (
                        summarizer.summarize(
                            _PULL_PROMPT.format(title=title, body=body or "(no body)")
                        )
                        or ""
                    )
                    row += f" {summary.replace('|', chr(92) + '|')} |"
                fh.write(row + "\n")
            fh.write("\n")

        totals = (
            f"{total_open} open, {total_merged} merged, "
            f"{total_closed} closed without merging"
        )
        if total_unknown:
            totals += f", {total_unknown} of unrecognised state"
        fh.write(f"\n_Totals: {totals}_\n")

    log(f"Wrote pulls digest: {totals}", verbose, level=LogLevel.STATUS)
    return out_path
