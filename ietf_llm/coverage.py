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

Unwindowed is not unbounded, which is the third constraint and the reason
`RepoRecord` exists. "The full set" is true of a GitHub archive at the moment
it was built and false from then on, so a `grep_corpus` zero over `issues/` is
bounded above by a number nothing used to state — the difference between "no
one raised this" and "no one raised this in the part of the record we hold".
The bound is reported as the highest issue/PR number present, with the
archive's own build date rather than the gather date: the archive is fetched
from the repo's published `archive.json` when there is one, so it can be days
older than the gather that pulled it, and stamping it with the gather date
would overstate how current the ceiling is.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .config import load as load_config
from .freshness import last_gathered
from .months import DEFAULT_MONTHS

#: The gather config persists under this scope (matches `corpus._GATHER_SCOPE`).
_GATHER_SCOPE = "gather"

#: Days per month for deriving the window start. Matches the gather cutoff
#: (`30 * months`); approximate, which is why the rendered date is prefixed
#: with `~`.
_DAYS_PER_MONTH = 30


@dataclass
class RepoRecord:
    """One tracked GitHub repo, and where the record we hold for it ends.

    `ceiling` is the highest issue *or* PR number in the archive: GitHub
    numbers both in one sequence, so a per-kind ceiling would name the edge of
    the wrong record (on httpwg/http-extensions the issues stop at #3501 while
    the PRs reach #3502). `built` is the archive's own timestamp — see the
    module docstring on why not the gather date."""

    repo: str  # verbatim owner/repo
    ceiling: Optional[int]  # highest issue/PR number held; None if unreadable
    built: Optional[str]  # YYYY-MM-DD the archive was built, where stated


@dataclass
class Sources:
    """Which substantive sources are present in a corpus's files dir, detected
    from on-disk artifacts. Charter / group metadata are omitted — they are
    near-universal and low-signal; this is about the queryable content."""

    mailing_list: bool
    repos: List[str]  # GitHub issue repos, verbatim owner/repo, sorted; empty
    # on the compact path (which counts archives rather than parsing names)
    repo_count: int  # tracked GitHub issue repos — `len(repos)` on the full
    # path, a filename count on the compact one (where `repos` is unfilled)
    drafts: bool
    rfcs: bool
    meetings: bool
    records: List[RepoRecord] = field(default_factory=list)  # same repos as
    # `repos`, with their record ceilings; full path only, for the same reason


# --- Window -----------------------------------------------------------------


def window_months(wg: str) -> int:
    """The gather window in months for `wg`. `0` means an all-history gather
    (`--months 0`), which is unbounded — callers treat it as "no floor".

    A default-window gather doesn't persist `months` (config only writes back
    non-default scalars), so an absent value means the default was used. A
    persisted `0` is preserved (it is *not* the default), so an all-history
    corpus isn't mis-reported as a 12-month window.
    """
    months = load_config(wg, _GATHER_SCOPE).get("months")
    if isinstance(months, int) and months >= 0:
        return months
    return DEFAULT_MONTHS


def _start_label(wg: str, months: int) -> Optional[str]:
    """`coverage_start_label` body given an already-resolved `months`, so a
    caller that also needs `months` reads the (cloud control-plane) config once
    rather than twice."""
    if months == 0:
        return None
    when = last_gathered(wg)
    if when is None:
        return None
    start = when - timedelta(days=_DAYS_PER_MONTH * months)
    return start.strftime("%Y-%m")


def coverage_start_label(wg: str) -> Optional[str]:
    """`YYYY-MM` for the start of the windowed coverage (gather date minus the
    window), or None when there's no gather record to anchor it — or when the
    window is unbounded (`months == 0`, all history), which has no floor to
    report."""
    return _start_label(wg, window_months(wg))


# --- Source detection -------------------------------------------------------


#: An archive timestamp we'll quote. The gh-pages archives carry an ISO
#: instant (`2026-08-06T01:49:14.562886+00:00`); the API fallback writes a
#: naive `datetime.now().isoformat()`. Only the date part is reported, so both
#: shapes reduce to the same thing and anything else is dropped rather than
#: rendered as a bogus date.
_ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _archive_ceiling(data: Dict[str, Any]) -> Optional[int]:
    """The highest issue/PR number in one parsed archive, or None when it holds
    no numbered record. Both arrays are scanned — see `RepoRecord.ceiling`."""
    top: Optional[int] = None
    for key in ("issues", "pulls"):
        records = data.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            number = record.get("number") if isinstance(record, dict) else None
            if isinstance(number, int) and (top is None or number > top):
                top = number
    return top


def github_records(files_dir: str) -> List[RepoRecord]:
    """One `RepoRecord` per `github/<slug>.json` archive — the verbatim repo
    name (the non-lossy source of truth; the dir slug replaces `/` with `-`),
    the highest issue/PR number held, and when the archive was built. Sorted by
    repo name, one archive parse each; empty when no archives are present.

    `github_repos` is the name-only view of this, and `github_repo_count` the
    count-only one that skips the parse entirely.
    """
    gh_dir = paths.github_dir(files_dir)
    try:
        names = os.listdir(gh_dir)
    except OSError:
        return []
    by_repo: Dict[str, RepoRecord] = {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(gh_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        repo = data.get("repo")
        if not isinstance(repo, str) or not repo:
            continue
        stamp = data.get("timestamp")
        match = _ISO_DATE_RE.match(stamp) if isinstance(stamp, str) else None
        by_repo[repo] = RepoRecord(
            repo=repo,
            ceiling=_archive_ceiling(data),
            built=match.group(1) if match else None,
        )
    return [by_repo[repo] for repo in sorted(by_repo)]


def github_repos(files_dir: str) -> List[str]:
    """The verbatim `owner/repo` names whose issues are present, read from the
    `repo` field of each `github/<slug>.json` archive (the non-lossy source of
    truth). Sorted and de-duplicated; empty when no archives are present."""
    return [record.repo for record in github_records(files_dir)]


def github_repo_count(files_dir: str) -> int:
    """How many GitHub issue archives are present — one `github/<slug>.json`
    per tracked repo — counted from filenames without parsing them. The compact
    `list_corpora` inventory and the windowed coverage line need only the count,
    so they skip the full `json.load` that `github_repos` does to recover the
    verbatim names (a parse of every archive, on every tool response)."""
    gh_dir = paths.github_dir(files_dir)
    try:
        return sum(1 for name in os.listdir(gh_dir) if name.endswith(".json"))
    except OSError:
        return 0


def detect_sources(files_dir: str) -> Sources:
    """Full inventory of `files_dir`, on-disk only — including verbatim GitHub
    repo names (a parse of every archive). Use `detect_sources_compact` when
    only presence and counts are needed (the windowed line, `list_corpora`)."""
    has_drafts, has_rfcs = _draft_kinds(paths.drafts_dir(files_dir))
    records = github_records(files_dir)
    return Sources(
        mailing_list=_dir_nonempty(paths.threads_dir(files_dir)),
        repos=[record.repo for record in records],
        repo_count=len(records),
        drafts=has_drafts,
        rfcs=has_rfcs,
        meetings=_dir_nonempty(paths.meetings_dir(files_dir)),
        records=records,
    )


def detect_sources_compact(files_dir: str) -> Sources:
    """Cheap inventory: presence flags plus a GitHub repo *count* by filename,
    never parsing archives for names. Drives the windowed coverage line (on
    every tool response) and the `list_corpora` column, where verbatim repo
    names aren't shown — only `overview` needs those, via `detect_sources`."""
    has_drafts, has_rfcs = _draft_kinds(paths.drafts_dir(files_dir))
    return Sources(
        mailing_list=_dir_nonempty(paths.threads_dir(files_dir)),
        repos=[],
        repo_count=github_repo_count(files_dir),
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


def _fullset_clause(sources: Sources) -> str:
    """The trailing `; … not windowed` caveat, naming only the non-windowed
    sources actually present (GitHub issues, drafts) — empty when the corpus
    has neither, so the line never cites a source it lacks.

    It says where they end rather than calling them "the full set": unwindowed
    means the window doesn't bound them, not that nothing does. `overview`
    carries the numeric edge (`record_edge_line`); this line runs on every tool
    response, so it stays cheap and says only which bound applies."""
    full: List[str] = []
    if sources.repo_count:
        full.append("GitHub issues/PRs")
    if sources.drafts:
        full.append("drafts")
    if not full:
        return ""
    return (
        f"; {' and '.join(full)} are not windowed, but end where the last "
        "gather did — not where the record does"
    )


def record_edge_line(records: List[RepoRecord]) -> str:
    """Where each tracked repo's record stops: `owner/repo through #N (archive
    built YYYY-MM-DD)`, joined by `;`. Empty when nothing is known.

    This is what makes a `grep_corpus` zero over `issues/` / `pulls/` a bounded
    claim instead of an open one — the reader can see that #3504 was never
    scanned because it was never fetched."""
    parts: List[str] = []
    for record in records:
        if record.ceiling is None:
            continue
        built = f" (archive built {record.built})" if record.built else ""
        parts.append(f"{record.repo} through #{record.ceiling}{built}")
    return "; ".join(parts)


def window_line(
    wg: str, files_dir: str, *, sources: Optional[Sources] = None
) -> Optional[str]:
    """A one-line italic coverage floor for top-level tool responses, or None
    when there's no gather record or nothing windowed to report. Detects sources
    cheaply (no archive parse) unless a precomputed `sources` is passed in."""
    months = window_months(wg)  # one config read; reused for the window clause
    start = _start_label(wg, months)
    if start is None:
        return None
    src = sources or detect_sources_compact(files_dir)
    subject = _windowed_subject(src)
    if subject is None:
        return None
    return (
        f"_Coverage: {subject} reaches back to ~{start} "
        f"({months}-mo window){_fullset_clause(src)}._"
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
    (`list · issues×2 · drafts · RFCs · minutes`). Empty when nothing present.
    Detects cheaply (counts archives, never parses them) unless a precomputed
    `sources` is passed in."""
    src = sources or detect_sources_compact(files_dir)
    parts: List[str] = []
    if src.mailing_list:
        parts.append("list")
    if src.repo_count:
        parts.append(f"issues×{src.repo_count}" if src.repo_count > 1 else "issues")
    if src.drafts:
        parts.append("drafts")
    if src.rfcs:
        parts.append("RFCs")
    if src.meetings:
        parts.append("minutes")
    return " · ".join(parts)
