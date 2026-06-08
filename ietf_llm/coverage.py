"""Reader-side coverage descriptor: how far back a gather reaches and which
sources it contains.

A client that finds a corpus gathered for only a short window has no way to
know it must re-gather deeper to answer a question about older activity —
the freshness line tells it when the gather *ended* but not when its window
*starts*, nor what sources are inside. This module fills that gap, derived
entirely from on-disk artifacts plus the persisted gather window: no network,
no model, no re-gather. Existing caches benefit immediately.

Two honesty constraints shape what we report:

  - The `months` window only bounds the *recency* of the mailing list and
    meeting activity (and the new-drafts cutoff). GitHub issues are pulled in
    full (entire history) and drafts/RFCs are the full active set — neither is
    windowed. So the window phrase names only the windowed sources, and the
    source inventory makes no window claim.
  - Which GitHub repos a corpus tracks is read from each archive's `repo`
    field (the verbatim `owner/repo`), not the on-disk slug dir name — the
    slug replaces `/` with `-` and is lossy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Tuple

from . import paths
from .config import load as load_config
from .freshness import last_gathered
from .utils import DEFAULT_MONTHS

#: The gather config persists under this scope (matches `corpus._GATHER_SCOPE`).
_GATHER_SCOPE = "gather"

#: Days per month for deriving the window start. Matches the gather cutoff
#: (`30 * months`); approximate, which is why the rendered date is prefixed
#: with `~`.
_DAYS_PER_MONTH = 30


@dataclass
class Sources:
    """Which substantive sources are present in a corpus's files dir, detected
    from on-disk artifacts. Charter / group metadata are omitted — they are
    near-universal and low-signal; this is about the queryable content."""

    mailing_list: bool
    repos: List[str]  # GitHub issue repos, verbatim owner/repo, sorted
    drafts: bool
    rfcs: bool
    meetings: bool


# --- Window -----------------------------------------------------------------


def window_months(wg: str) -> int:
    """The gather window in months for `wg`.

    A default-window gather doesn't persist `months` (config only writes back
    non-default scalars), so an absent value means the default was used.
    """
    months = load_config(wg, _GATHER_SCOPE).get("months")
    if isinstance(months, int) and months > 0:
        return months
    return DEFAULT_MONTHS


def coverage_start_label(wg: str) -> Optional[str]:
    """`YYYY-MM` for the start of the windowed coverage (gather date minus the
    window), or None when there's no gather record to anchor it."""
    when = last_gathered(wg)
    if when is None:
        return None
    start = when - timedelta(days=_DAYS_PER_MONTH * window_months(wg))
    return start.strftime("%Y-%m")


# --- Source detection -------------------------------------------------------


def github_repos(files_dir: str) -> List[str]:
    """The verbatim `owner/repo` names whose issues are present, read from the
    `repo` field of each `github/<slug>.json` archive (the non-lossy source of
    truth). Sorted and de-duplicated; empty when no archives are present."""
    gh_dir = paths.github_dir(files_dir)
    try:
        names = os.listdir(gh_dir)
    except OSError:
        return []
    repos: set[str] = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(gh_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        repo = data.get("repo") if isinstance(data, dict) else None
        if isinstance(repo, str) and repo:
            repos.add(repo)
    return sorted(repos)


def detect_sources(files_dir: str) -> Sources:
    """Inventory the substantive sources present in `files_dir`, on-disk only."""
    has_drafts, has_rfcs = _draft_kinds(paths.drafts_dir(files_dir))
    return Sources(
        mailing_list=_dir_nonempty(paths.threads_dir(files_dir)),
        repos=github_repos(files_dir),
        drafts=has_drafts,
        rfcs=has_rfcs,
        meetings=_dir_nonempty(paths.meetings_dir(files_dir)),
    )


def _dir_nonempty(path: str) -> bool:
    try:
        return any(True for _ in os.scandir(path))
    except OSError:
        return False


def _draft_kinds(drafts_dir: str) -> Tuple[bool, bool]:
    """`(has_drafts, has_rfcs)` from the shared `drafts/` dir in one scan."""
    has_drafts = has_rfcs = False
    try:
        for entry in os.scandir(drafts_dir):
            if entry.name.startswith("draft-"):
                has_drafts = True
            elif entry.name.startswith("rfc"):
                has_rfcs = True
            if has_drafts and has_rfcs:
                break
    except OSError:
        pass
    return has_drafts, has_rfcs


# --- Renderers --------------------------------------------------------------


def _windowed_subject(sources: Sources) -> Optional[str]:
    """The clause naming whichever windowed sources are present, or None.

    Only the mailing list and meetings are bounded by the window, so a corpus
    with neither (e.g. a draft- or issue-only custom corpus) has no window to
    report — issues and drafts are full-set."""
    parts: List[str] = []
    if sources.mailing_list:
        parts.append("mailing-list")
    if sources.meetings:
        parts.append("meeting")
    if not parts:
        return None
    return " & ".join(parts) + " activity"


def window_line(
    wg: str, files_dir: str, *, sources: Optional[Sources] = None
) -> Optional[str]:
    """A one-line italic coverage floor for top-level tool responses, or None
    when there's no gather record or nothing windowed to report."""
    start = coverage_start_label(wg)
    if start is None:
        return None
    subject = _windowed_subject(sources or detect_sources(files_dir))
    if subject is None:
        return None
    return (
        f"_Coverage: {subject} reaches back to ~{start} "
        f"({window_months(wg)}-mo window); GitHub issues and drafts are the "
        "full set, not windowed._"
    )


def _join_repos(repos: List[str], limit: Optional[int]) -> str:
    if limit is None or len(repos) <= limit:
        return ", ".join(repos)
    return ", ".join(repos[:limit]) + f", +{len(repos) - limit} more"


def sources_line(
    files_dir: str,
    *,
    repo_limit: Optional[int] = None,
    sources: Optional[Sources] = None,
) -> str:
    """A `·`-separated inventory naming the present sources, with GitHub repos
    listed by name (capped at `repo_limit`). Empty string when nothing is
    present."""
    src = sources or detect_sources(files_dir)
    parts: List[str] = []
    if src.mailing_list:
        parts.append("mailing list")
    if src.repos:
        parts.append(f"GitHub issues ({_join_repos(src.repos, repo_limit)})")
    if src.drafts:
        parts.append("drafts")
    if src.rfcs:
        parts.append("RFCs")
    if src.meetings:
        parts.append("minutes")
    return " · ".join(parts)


def compact_sources_line(files_dir: str, *, sources: Optional[Sources] = None) -> str:
    """A terse inventory for the `list_corpora` table — counts, not repo names
    (`list · issues×2 · drafts · RFCs · minutes`). Empty when nothing present."""
    src = sources or detect_sources(files_dir)
    parts: List[str] = []
    if src.mailing_list:
        parts.append("list")
    if src.repos:
        parts.append(f"issues×{len(src.repos)}" if len(src.repos) > 1 else "issues")
    if src.drafts:
        parts.append("drafts")
    if src.rfcs:
        parts.append("RFCs")
    if src.meetings:
        parts.append("minutes")
    return " · ".join(parts)
