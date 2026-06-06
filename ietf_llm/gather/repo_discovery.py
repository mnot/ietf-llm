"""Discover the GitHub repos worth tracking for a Datatracker group.

A Working Group's Datatracker "Additional Resources" usually name a GitHub
organisation (the `github_org` slug, e.g. httpbis → ``https://github.com/
httpwg/``) and occasionally a direct repo (`github_repo`). An org holds
many repos, most of which are *not* where drafts are discussed — wikis,
themes, admin, test suites, archived mirrors. This module narrows that set
to the repos a gather should actually follow: those that both (1) carry
Internet-Draft sources (the `draft-*.{md,xml,…}` files MT's i-d-template
lays down in the repo root) and (2) have an active issue tracker.

The result feeds two paths:
- gather auto-includes the **high-confidence** matches (repo files
  intersect the group's *currently-active* drafts AND issues are live) the
  first time a group-backed corpus is gathered with no `--github` set;
- the `suggest_github_repos` MCP tool / `--discover-github` CLI flag print
  the full ranked list for a human to choose from.

Request budget matters — unauthenticated GitHub is 60 requests/hour — so
the cheap org-listing fields (`has_issues` / `open_issues_count` /
`pushed_at` / `archived` / `fork`) pre-filter to a handful before any
per-repo contents/issues probe, and `GITHUB_TOKEN` is honoured. Anything
dropped by a cap is logged, never silently truncated.
"""

import argparse
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .. import config
from ..utils import (
    DEFAULT_HEADERS,
    LogLevel,
    Verbosity,
    get_group_resources,
    http_session,
    log,
)
from .drafts import get_wg_documents

#: How recently a repo must have been pushed to (and have issue activity) to
#: count as live. A draft repo quiet for this long is treated as finished.
DEFAULT_STALE_MONTHS = 18

#: Cap on repos listed from a single org, and on repos probed per group.
#: Bounds request volume for orgs with very many repos.
MAX_ORG_REPOS = 200
MAX_PROBE = 40

#: A draft source file in the i-d-template convention: ``draft-…`` with a
#: kramdown-rfc / xml2rfc / org extension. The stem (group 1) is matched
#: against the group's active draft names.
_DRAFT_FILE_RE = re.compile(
    r"^(draft-[a-z0-9][a-z0-9-]*)\.(?:md|xml|org|txt)$", re.IGNORECASE
)

#: Draft-name suffixes that don't appear in Datatracker names but do on disk
#: (a working copy revision, or the i-d-template ``-latest`` build target).
_DRAFT_SUFFIX_RE = re.compile(r"-(?:latest|\d{2})$", re.IGNORECASE)


@dataclass
class RepoCandidate:
    """One repo considered for tracking, with the signals discovery found."""

    full_name: str
    has_issues: bool
    open_issues: int
    pushed_at: str
    archived: bool
    fork: bool
    draft_files: List[str] = field(default_factory=list)
    wg_draft_matches: List[str] = field(default_factory=list)
    last_issue_activity: Optional[str] = None
    issues_active: bool = False

    @property
    def has_drafts(self) -> bool:
        return bool(self.draft_files)

    @property
    def high_confidence(self) -> bool:
        """Draft sources that match a *currently-active* WG draft, plus a
        live issue tracker — the bar for auto-inclusion in a gather."""
        return bool(self.wg_draft_matches) and self.issues_active


@dataclass
class DiscoveryResult:
    """The outcome of discovery for one group."""

    wg: str
    candidates: List[RepoCandidate] = field(default_factory=list)
    high_confidence: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    note: Optional[str] = None
    #: True when a GitHub throttle / outage left the scan incomplete, so a
    #: "nothing found" outcome is inconclusive and shouldn't disable retry.
    incomplete: bool = False


class _GhClient:
    """A thin GitHub API GET wrapper that records transient incidents.

    Distinguishing "GitHub said no such repo" (404) from "GitHub throttled
    or was unreachable" matters: the former is a real negative, the latter
    means the discovery result is incomplete and shouldn't be reported as
    "nothing found". Rate-limit (403/429) and transport failures are
    accumulated in `incidents` so the caller can caveat its output (and
    suggest `GITHUB_TOKEN`, which lifts the 60/hr unauthenticated cap).
    """

    def __init__(self, token: Optional[str]) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or None
        self.incidents: "set[str]" = set()

    def get(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> "Tuple[Optional[int], Any]":
        """GET a GitHub API URL. Returns ``(status_code, parsed_json)``.

        ``status_code`` is None on a transport error. ``parsed_json`` is
        None on any non-200 or unparseable body. Rate-limit / unreachable
        outcomes are also recorded in `self.incidents`.
        """
        headers = {**DEFAULT_HEADERS, "Accept": "application/vnd.github.v3+json"}
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        try:
            resp = http_session().get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException:
            self.incidents.add("unreachable")
            return None, None
        if resp.status_code in (403, 429):
            self.incidents.add("rate_limited")
            return resp.status_code, None
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError:
            return 200, None


def _owner_from_org_url(value: str) -> str:
    """Reduce a `github_org` resource value to its bare owner login."""
    cleaned = value.strip().rstrip("/")
    if cleaned.lower().startswith(("http://", "https://")):
        cleaned = cleaned.split("/")[-1]
    return cleaned


def _normalize_repo(value: str) -> str:
    """Reduce a `github_repo` resource value to ``owner/repo`` (URL or short)."""
    cleaned = value.strip().rstrip("/")
    if cleaned.lower().startswith(("http://", "https://")):
        cleaned = "/".join(cleaned.split("/")[-2:])
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned


def _parse_resources(
    resources: "Tuple[Tuple[str, str, str], ...]",
) -> "Tuple[List[str], List[str]]":
    """Split a group's Additional Resources into (org owners, direct repos)."""
    owners: List[str] = []
    repos: List[str] = []
    for slug, _label, value in resources:
        if slug == "github_org" and value:
            owner = _owner_from_org_url(value)
            if owner and owner not in owners:
                owners.append(owner)
        elif slug == "github_repo" and value:
            repo = _normalize_repo(value)
            if "/" in repo and repo not in repos:
                repos.append(repo)
    return owners, repos


def _incident_note(incidents: "set[str]") -> Optional[str]:
    """A caveat string for the transient incidents a scan hit, or None."""
    if not incidents:
        return None
    if "rate_limited" in incidents:
        return (
            "Some GitHub requests were rate-limited, so this list may be "
            "incomplete — set GITHUB_TOKEN for a higher limit, or retry later."
        )
    return (
        "Some GitHub requests failed (network), so this list may be "
        "incomplete — retry later."
    )


def _months_ago_iso(months: int) -> str:
    """An ISO-8601 UTC timestamp `months` ago, comparable to GitHub's
    `pushed_at` / `updated_at` lexicographically (same fixed format)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_owner_repos(
    client: _GhClient,
    owner: str,
    max_repos: int,
    verbose: Verbosity,
) -> List[Dict[str, Any]]:
    """List an owner's public repos, trying the org endpoint then the user
    endpoint (a `github_org` value is sometimes a personal account).

    Stops on the first throttle/transport failure rather than wasting the
    second attempt on the same outage — the incident is recorded on
    `client` for the caller to surface.
    """
    for kind in ("orgs", "users"):
        repos: List[Dict[str, Any]] = []
        page = 1
        while len(repos) < max_repos:
            status, data = client.get(
                f"https://api.github.com/{kind}/{owner}/repos",
                params={"per_page": 100, "page": page, "sort": "pushed"},
            )
            if status == 404:
                break  # not this kind of account; try the next
            if status in (403, 429) or status is None:
                return repos  # throttled / unreachable; incident recorded
            if not isinstance(data, list) or not data:
                return repos
            repos.extend(data)
            if len(data) < 100:
                return repos
            page += 1
        if repos:
            if len(repos) > max_repos:
                log(
                    f"discover: {owner} has more than {max_repos} repos; "
                    "considering the most-recently-pushed only.",
                    verbose,
                    level=LogLevel.STATUS,
                )
            return repos[:max_repos]
    return []


def _candidate_from_obj(obj: Dict[str, Any]) -> RepoCandidate:
    return RepoCandidate(
        full_name=obj.get("full_name") or "",
        has_issues=bool(obj.get("has_issues")),
        open_issues=int(obj.get("open_issues_count") or 0),
        pushed_at=obj.get("pushed_at") or "",
        archived=bool(obj.get("archived")),
        fork=bool(obj.get("fork")),
    )


def _passes_prefilter(cand: RepoCandidate, stale_before: str) -> bool:
    """Cheap reject using only org-listing fields, before any per-repo call."""
    if not cand.full_name or cand.archived or cand.fork or not cand.has_issues:
        return False
    if cand.pushed_at and cand.pushed_at < stale_before:
        return False
    return True


def _match_key(stem: str) -> str:
    """Normalise a draft stem / name for matching (lowercase, drop a
    trailing working-revision or ``-latest`` build suffix)."""
    return _DRAFT_SUFFIX_RE.sub("", stem.lower())


def _draft_files(client: _GhClient, full_name: str) -> List[str]:
    """Names of `draft-*` source files in the repo root, sorted."""
    owner, _, repo = full_name.partition("/")
    _status, data = client.get(f"https://api.github.com/repos/{owner}/{repo}/contents/")
    if not isinstance(data, list):
        return []
    out = [
        entry.get("name") or ""
        for entry in data
        if entry.get("type") == "file" and _DRAFT_FILE_RE.match(entry.get("name") or "")
    ]
    return sorted(out)


def _last_issue_activity(client: _GhClient, full_name: str) -> Optional[str]:
    """`updated_at` of the most recently touched *issue* (not PR), or None.

    The issues endpoint returns PRs too, so a small page is fetched and PRs
    (which carry a `pull_request` key) are skipped — `open_issues_count`
    alone counts PRs and can't tell a live tracker from an open PR backlog.
    """
    owner, _, repo = full_name.partition("/")
    _status, data = client.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        params={"state": "all", "sort": "updated", "direction": "desc", "per_page": 20},
    )
    if not isinstance(data, list):
        return None
    for item in data:
        if not item.get("pull_request"):
            updated = item.get("updated_at")
            return updated if isinstance(updated, str) else None
    return None


def _live_wg_draft_keys(wg: str, verbose: Verbosity) -> "set[str]":
    """Match-keys of the group's currently-active adopted drafts.

    Drives the high-confidence test: a repo whose draft files include an
    active WG draft is almost certainly *the* repo. Expired / published /
    replaced drafts are excluded so a repo of finished work doesn't qualify.
    """
    docs = get_wg_documents(wg, verbose=verbose)
    keys: "set[str]" = set()
    for draft in docs.get("drafts", []):
        # state None == API couldn't classify; keep it (embed-safe default).
        if draft.get("state") in (None, "active"):
            name = draft.get("name") or ""
            if name:
                keys.add(_match_key(name))
    return keys


def _rank_key(cand: RepoCandidate) -> "Tuple[bool, bool, int, int, str]":
    return (
        cand.high_confidence,
        cand.issues_active,
        len(cand.wg_draft_matches),
        len(cand.draft_files),
        cand.pushed_at,
    )


def discover_group_repos(
    wg: str,
    token: Optional[str] = None,
    months: int = DEFAULT_STALE_MONTHS,
    max_repos: int = MAX_ORG_REPOS,
    max_probe: int = MAX_PROBE,
    live_draft_keys: "Optional[set[str]]" = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> DiscoveryResult:
    """Find the GitHub repos worth tracking for Datatracker group `wg`.

    Reads the group's `github_org` / `github_repo` Additional Resources,
    lists the org's repos, pre-filters on the cheap listing fields, then
    probes survivors for draft sources and issue activity. Returns a
    `DiscoveryResult` with the ranked candidates plus the `high_confidence`
    set (auto-include bar) and lower-confidence `suggestions`.

    Pure network failures degrade to fewer/no candidates, never an error —
    discovery is advisory and must not break a gather.
    """
    client = _GhClient(token)
    owners, direct = _parse_resources(get_group_resources(wg))
    if not owners and not direct:
        return DiscoveryResult(
            wg=wg, note="Datatracker lists no GitHub org or repo for this group."
        )

    objs: List[Dict[str, Any]] = []
    for owner in owners:
        objs.extend(_list_owner_repos(client, owner, max_repos, verbose))
    for repo in direct:
        _status, obj = client.get(f"https://api.github.com/repos/{repo}")
        if isinstance(obj, dict) and obj.get("full_name"):
            objs.append(obj)

    # Dedupe (a direct repo may also appear in its org listing).
    seen: "set[str]" = set()
    unique: List[Dict[str, Any]] = []
    for obj in objs:
        full = (obj.get("full_name") or "").lower()
        if full and full not in seen:
            seen.add(full)
            unique.append(obj)

    stale_before = _months_ago_iso(months)
    survivors = [
        cand
        for cand in (_candidate_from_obj(obj) for obj in unique)
        if _passes_prefilter(cand, stale_before)
    ]
    survivors.sort(key=lambda cand: cand.pushed_at, reverse=True)
    if len(survivors) > max_probe:
        log(
            f"discover {wg}: {len(survivors)} active repos; probing the "
            f"{max_probe} most-recently-pushed.",
            verbose,
            level=LogLevel.STATUS,
        )
        survivors = survivors[:max_probe]

    if live_draft_keys is None:
        live_draft_keys = _live_wg_draft_keys(wg, verbose)

    for cand in survivors:
        cand.draft_files = _draft_files(client, cand.full_name)
        if not cand.draft_files:
            continue
        stems = set()
        for fname in cand.draft_files:
            match = _DRAFT_FILE_RE.match(fname)
            if match:
                stems.add(_match_key(match.group(1)))
        cand.wg_draft_matches = sorted(stems & live_draft_keys)
        cand.last_issue_activity = _last_issue_activity(client, cand.full_name)
        cand.issues_active = bool(
            cand.last_issue_activity and cand.last_issue_activity >= stale_before
        )

    with_drafts = [cand for cand in survivors if cand.has_drafts]
    with_drafts.sort(key=_rank_key, reverse=True)
    return DiscoveryResult(
        wg=wg,
        candidates=with_drafts,
        high_confidence=[c.full_name for c in with_drafts if c.high_confidence],
        suggestions=[c.full_name for c in with_drafts if not c.high_confidence],
        note=_incident_note(client.incidents),
        incomplete=bool(client.incidents),
    )


def format_discovery(result: DiscoveryResult) -> str:
    """Render a `DiscoveryResult` as a human / agent-readable report."""
    if not result.candidates:
        # The note carries the reason (no org recorded, or a throttle); fall
        # back to a plain not-found line when there isn't one.
        reason = result.note or (
            "No repos with Internet-Draft sources and active issues were found "
            "among the group's GitHub org/repos."
        )
        return f"**{result.wg}** — {reason}"
    lines = [f"GitHub repos for **{result.wg}** (draft sources + issue tracker):", ""]
    for cand in result.candidates:
        mark = "✓ track" if cand.high_confidence else "  maybe"
        bits = [f"{len(cand.draft_files)} draft file(s)"]
        if cand.wg_draft_matches:
            bits.append(f"{len(cand.wg_draft_matches)} match active WG drafts")
        if cand.issues_active:
            bits.append(f"{cand.open_issues} open issues, active")
        elif cand.has_issues:
            bits.append("issues quiet")
        lines.append(f"- [{mark}] `{cand.full_name}` — {'; '.join(bits)}")
    lines.append("")
    if result.high_confidence:
        joined = ", ".join(f'"{r}"' for r in result.high_confidence)
        lines.append(
            f'Recommended: `start_gather(corpus="{result.wg}", ' f"github=[{joined}])`."
        )
    else:
        lines.append(
            "Nothing met the auto-track bar (active WG draft + live issues); "
            "the 'maybe' repos above can be added with `github=[...]` if wanted."
        )
    if result.note:  # a throttle caveat alongside whatever was found
        lines += ["", f"_{result.note}_"]
    return "\n".join(lines)


def print_discovery(wg: str, verbose: Verbosity = Verbosity.STATUS) -> int:
    """Print the repos discovery recommends tracking for `wg`, return 0.

    A dry run for the `--discover-github` CLI flag: no gather, no config
    written.
    """
    print(format_discovery(discover_group_repos(wg, verbose=verbose)))
    return 0


def autotrack_github(
    args: argparse.Namespace,
    persisted: Dict[str, Any],
    group_backed: bool,
    scope: str,
    verbose: Verbosity,
) -> None:
    """On a group-backed corpus's first gather with no `--github` set, fold
    the high-confidence discovered repos into `args.github` so they're
    tracked from the start.

    Runs **once per corpus**: a `github_discovered` marker is written to the
    config afterwards, so a later gather never re-adds a repo the user has
    since removed — they own the `github` set from then on. Re-run discovery
    explicitly with `--discover-github` or the `suggest_github_repos` MCP
    tool. Called before `config.merge`, which re-reads and re-saves the
    config (preserving the marker alongside any `github` list it persists
    from `args.github`).

    A scan left **incomplete** by a GitHub throttle / outage does *not* burn
    the one-shot: the marker is withheld so the next gather retries.
    """
    if not group_backed:
        return
    # Skip when the user already controls the github set this run, has one
    # persisted, or discovery has already run once for this corpus.
    if args.github or persisted.get("github") or persisted.get("github_discovered"):
        return

    result = discover_group_repos(args.wg, verbose=verbose)
    if result.high_confidence:
        args.github = list(result.high_confidence)
        log(
            f"{args.wg}: auto-tracking {len(result.high_confidence)} GitHub "
            f"repo(s) from discovery: {', '.join(result.high_confidence)}.",
            verbose,
            level=LogLevel.STATUS,
        )
    if result.suggestions:
        log(
            f"{args.wg}: also found draft repos not auto-tracked: "
            f"{', '.join(result.suggestions)}. Add with `--github owner/repo` "
            "to include them.",
            verbose,
            level=LogLevel.STATUS,
        )
    if result.incomplete and not result.high_confidence:
        # Throttled with nothing to show — leave the marker unset so the next
        # gather tries again rather than permanently skipping discovery.
        log(
            f"{args.wg}: GitHub repo discovery was throttled; will retry on the "
            "next gather. Set GITHUB_TOKEN to avoid the unauthenticated limit.",
            verbose,
            level=LogLevel.STATUS,
        )
        return
    marker_cfg = config.load(args.wg, scope)
    marker_cfg["github_discovered"] = True
    config.save(args.wg, scope, marker_cfg)
