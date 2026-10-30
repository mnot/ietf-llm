import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import requests

from ... import paths
from ...atomicio import atomic_open
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get
from ...net.transport import RETRY_AFTER_MAX
from .github_schema import load_archive, normalise_archive, valid_repo


def iter_issue_archives(archives_dir: str) -> "Iterator[Dict[str, Any]]":
    """Yield each parsed `<repo>.json` issue archive under `archives_dir`.

    Skips non-JSON files and any archive that fails to read / parse, and
    yields each in the normalised shape (see `github_schema`). Shared by the
    people registry and the timeline builder so they don't each re-implement
    the walk-and-load loop.
    """
    if not os.path.isdir(archives_dir):
        return
    for name in sorted(os.listdir(archives_dir)):
        if not name.endswith(".json"):
            continue
        data = load_archive(os.path.join(archives_dir, name), level=LogLevel.WARN)
        if data is not None:
            yield data


def label_descriptions(archives_dir: str) -> Dict[str, str]:
    """Map every defined GitHub label name to its description.

    The i-d-template archive carries a repo-level `labels` array
    (`{name, description, color}`) alongside `issues` and `pulls`. Issues
    and PRs record only the bare label *names*, so this is the only place
    the meaning of a tag like `ready to close` is written down. Colours
    are dropped — nothing here renders them.

    Labels with no description are included with an empty string, so a
    caller can distinguish "defined but undescribed" from "not defined
    here" (a label deleted from the repo after being applied still shows
    up on old issues). Later archives win on a name collision across
    repos; the descriptions are conventional enough across a WG's repos
    that picking one is better than rendering both.
    """
    out: Dict[str, str] = {}
    for data in iter_issue_archives(archives_dir):
        for label in data.get("labels") or []:
            if not isinstance(label, dict):
                continue
            name = label.get("name")
            if not isinstance(name, str) or not name:
                continue
            description = label.get("description")
            out[name] = description.strip() if isinstance(description, str) else ""
    return out


def format_date(iso_date: Optional[str]) -> str:
    """Convert ISO date to a more readable format."""
    if not iso_date:
        return "(Unknown Date)"
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return iso_date


def process_github_issues(
    input_file: str,
    output_file: str,
    include_labels: Optional[List[str]] = None,
    exclude_labels: Optional[List[str]] = None,
    verbose: Verbosity = Verbosity.STATUS,
    registry: "Optional[object]" = None,
) -> List[str]:
    """Process a GitHub issues JSON archive and write cleaned text to output_file.

    If `registry` is supplied (an `ietf_llm.people.Registry`), the
    Author / Comment-by lines are written using canonical names —
    so the agent reading this file sees "Mark Nottingham" instead
    of the raw `mnot` GitHub login.
    """

    def _canon(login: str) -> str:
        if registry is None or not login:
            return login
        resolved = registry.canonical_for_github(login)  # type: ignore[attr-defined]
        return resolved or login

    log(f"Opening {input_file}...", verbose, level=LogLevel.PROGRESS)
    try:
        with open(input_file, "r", encoding="utf-8") as json_fh:
            data = json.load(json_fh)
    except (ValueError, RecursionError, OSError) as err:
        log(f"Error parsing GitHub JSON: {err}", verbose, level=LogLevel.ERROR)
        return []
    # Only displayed here (no path is built from it), so keep what it claims.
    claimed = data.get("repo") if isinstance(data, dict) else None
    repo_name = claimed if isinstance(claimed, str) else "Unknown Repo"
    data = normalise_archive(data, repo_name, os.path.basename(input_file), verbose)
    if data is None:
        return []

    issues = data.get("issues", [])

    processed_count = 0
    filtered_count = 0

    with atomic_open(output_file) as out_fh:
        out_fh.write(f"Repository: {repo_name}\n")
        out_fh.write(f"Archive Export Date: {format_date(data.get('timestamp'))}\n")
        out_fh.write("=" * 80 + "\n\n")

        for issue in issues:
            title = issue.get("title", "(No Title)")
            number = issue.get("number", "?")
            state = issue.get("state", "(Unknown State)")
            author = issue.get("author", "(Unknown Author)")
            created_at = format_date(issue.get("createdAt"))
            # A third-party gh-pages archive is ingested verbatim; coerce
            # labels to a list of strings so a malformed shape (objects, a
            # dict) degrades to no labels rather than raising on join/membership.
            issue_labels = [
                lbl for lbl in (issue.get("labels") or []) if isinstance(lbl, str)
            ]
            labels_str = ", ".join(issue_labels)
            body = (issue.get("body") or "").strip()

            # Inclusion filter: at least one label must match if include_labels is provided
            if include_labels:
                if not any(label in issue_labels for label in include_labels):
                    filtered_count += 1
                    continue

            # Exclusion filter: no label must match if exclude_labels is provided
            if exclude_labels:
                if any(label in issue_labels for label in exclude_labels):
                    filtered_count += 1
                    continue

            out_fh.write(f"Issue #{number}: {title}\n")
            out_fh.write(f"State: {state}\n")
            out_fh.write(f"Date: {created_at}\n")
            out_fh.write(f"Author: {_canon(author)}\n")
            if labels_str:
                out_fh.write(f"Labels: {labels_str}\n")
            out_fh.write("\n")

            out_fh.write((body or "(No description provided)") + "\n")

            comments = issue.get("comments", [])
            if comments:
                out_fh.write("\n" + "-" * 40 + "\n")
                out_fh.write(f"Comments ({len(comments)}):\n\n")
                for comment in comments:
                    c_author = comment.get("author", "(Unknown)")
                    c_date = format_date(comment.get("createdAt"))
                    c_body = (comment.get("body") or "").strip()

                    out_fh.write(f"--- Comment by {_canon(c_author)} on {c_date} ---\n")
                    out_fh.write(c_body + "\n\n")

            out_fh.write("=" * 80 + "\n\n")
            processed_count += 1

    msg = f"Done! Extracted {processed_count} issues to {output_file}."
    if filtered_count > 0:
        msg += f" ({filtered_count} issues filtered out by labels)"
    log(msg, verbose, level=LogLevel.STATUS)
    return [output_file]


def normalize_repo_short(value: str) -> str:
    """Reduce a `--github` value to its bare ``owner/repo`` short form.

    Accepts either a short name (``owner/repo``) or a full GitHub URL
    (``https://github.com/owner/repo[/...]``) and returns the last two
    path segments. A trailing slash or ``.git`` suffix is stripped. The
    URL test matches a real scheme, not any string starting with
    ``http`` — otherwise an owner like ``httpwg`` is mistaken for a URL.
    """
    cleaned = value.strip().rstrip("/")
    if cleaned.lower().startswith(("http://", "https://")):
        cleaned = "/".join(cleaned.split("/")[-2:])
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned


def validate_github_repos(
    values: List[str],
    verbose: Verbosity = Verbosity.STATUS,
    token: Optional[str] = None,
) -> List[str]:
    """Return the subset of `--github` `values` that name a real repo.

    Used by the CLI to drop typo'd `--github` values BEFORE
    `config.merge` persists them, mirroring `validate_draft_names` /
    `validate_list_names`. Each value is normalised to ``owner/repo``
    and probed against the GitHub repo API. Only a definitive 404 drops
    a value; an ambiguous failure (rate limit, network error) keeps it,
    so a transient outage does not discard working config. Values are
    returned in the user's original form so the persisted value matches
    what they typed.
    """
    valid: List[str] = []
    headers = {**DEFAULT_HEADERS, "Accept": "application/vnd.github.v3+json"}
    github_token = token or os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    for raw in values:
        short = normalize_repo_short(raw)
        owner, _, repo = short.partition("/")
        if not owner or not repo:
            log(
                f"--github {raw!r}: not an 'owner/repo' name; not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            resp = governed_get(api_url, headers=headers, timeout=30)
        except requests.RequestException:
            valid.append(raw)  # ambiguous — keep rather than discard config
            continue
        if resp.status_code == 404:
            log(
                f"--github {raw}: repository not found on GitHub; not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        valid.append(raw)
    return valid


def download_github_issues(
    repo_short: str,
    dest_path: str,
    token: Optional[str] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> bool:
    """Download GitHub issues JSON for an 'owner/repo' short name: the repo's gh-pages
    `archive.json` if it publishes one (normalised, see `github_schema`), else the API.
    """
    # Expecting owner/repo
    if not valid_repo(repo_short):
        log(
            f"Invalid GitHub short name: {repo_short}. Expected 'owner/repo'.",
            verbose,
            level=LogLevel.ERROR,
        )
        return False
    owner, repo = repo_short.split("/", 1)
    archive_url = f"https://{owner}.github.io/{repo}/archive.json"

    log(
        f"Checking for GitHub archive at {archive_url}...",
        verbose,
        level=LogLevel.STATUS,
    )
    try:
        response = governed_get(archive_url, headers=DEFAULT_HEADERS, timeout=30)
        if response.status_code == 200:
            log("Archive found; downloading...", verbose, level=LogLevel.STATUS)
            try:
                archive_data = response.json()
                # Ensure it's in our expected format (dict with 'issues' key)
                if isinstance(archive_data, list):
                    archive_data = {
                        "repo": f"{owner}/{repo}",
                        "timestamp": datetime.now().isoformat(),
                        "issues": archive_data,
                    }
                elif isinstance(archive_data, dict) and "issues" not in archive_data:
                    # If it's a dict but missing 'issues', we might still want to wrap it
                    # or handle it differently. For now, assume it might be a single issue
                    # or some other format and wrap if it's not our expected schema.
                    archive_data = {
                        "repo": f"{owner}/{repo}",
                        "timestamp": datetime.now().isoformat(),
                        "issues": [archive_data],
                    }
                archive_data = normalise_archive(
                    archive_data, repo_short, archive_url, verbose
                )
                if archive_data is not None:
                    with atomic_open(dest_path) as json_fh:
                        json.dump(archive_data, json_fh, indent=2)
                    return True
            except (ValueError, TypeError, RecursionError) as err:
                log(
                    f"Error parsing archive JSON: {err}",
                    Verbosity.VERBOSE,
                    level=LogLevel.STATUS,
                )
        log("No archive found on gh-pages.", verbose, level=LogLevel.PROGRESS)
    except (requests.RequestException, OSError) as err:
        log(
            f"Error checking gh-pages archive: {err}",
            verbose,
            level=LogLevel.STATUS,
        )

    log(
        f"Fetching GitHub issues via API for {owner}/{repo}...",
        verbose,
        level=LogLevel.STATUS,
    )
    headers = {**DEFAULT_HEADERS, "Accept": "application/vnd.github.v3+json"}
    github_token = token or os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    previous: Dict[Any, Dict[str, Any]] = {}
    if os.path.exists(dest_path):
        cached = load_archive(dest_path, verbose, level=LogLevel.PROGRESS)
        previous = {i["number"]: i for i in (cached or {}).get("issues", [])}
    report = _StaleReport()
    try:
        all_issues = _fetch_all_issues(
            owner, repo, headers, verbose, previous=previous, report=report
        )
        if report.stale:
            log(report.summary(f"{owner}/{repo}"), verbose, level=LogLevel.WARN)
        export_data = {
            "repo": f"{owner}/{repo}",
            "timestamp": datetime.now().isoformat(),
            "issues": all_issues,
        }
        with atomic_open(dest_path) as json_fh:
            json.dump(export_data, json_fh, indent=2)
        return True
    except (requests.RequestException, OSError) as err:
        log(f"Error fetching GitHub issues: {err}", verbose, level=LogLevel.ERROR)
        return False


def download_github_archives(
    repos: "Optional[List[str]]",
    cache_dir: str,
    verbosity: Verbosity,
    suppress_raw: bool = False,
) -> "List[tuple[str, str]]":
    """Download each configured repo's issue archive JSON. Returns
    `[(json_path, raw_txt_path)]` for the ones that downloaded, deferred
    so the .txt is rendered after the registry exists (canonical names).
    The `github/<repo>.json` archive (used by the issues digest) is always
    written; under `suppress_raw` the regenerable `raw/github-<repo>.txt`
    dump is not, so those entries drop from the returned list — and any
    such dump an earlier non-suppressed gather left behind is swept.

    Finally, any archive / raw dump / per-issue / per-PR dir belonging to a
    repo *not* in `repos` is pruned, so a corpus that drops a repo (or
    inherited files an older / leaked gather wrote for one it never tracked)
    heals on the next gather — symmetric with the thread and ballot stages.
    """
    pending: List[tuple[str, str]] = []
    normalized = [_normalize_repo(r) for r in repos or []]
    if normalized:
        os.makedirs(paths.github_dir(cache_dir), exist_ok=True)
        if not suppress_raw:
            os.makedirs(paths.raw_dir(cache_dir), exist_ok=True)
    for repo_short in normalized:
        gh_json = paths.github_archive_path(cache_dir, repo_short)
        gh_txt = paths.raw_github_text_path(cache_dir, repo_short)
        if suppress_raw and os.path.exists(gh_txt):
            # Sweep a dump an earlier non-suppressed gather left behind
            # so a cache migrated to the cloud backend doesn't carry
            # stale raw/ bulk forward (mirrors the .pdf sweep).
            _remove_quietly(gh_txt)
        if download_github_issues(repo_short, gh_json, verbose=verbosity):
            if not suppress_raw:  # raw/ text dump only; the JSON is kept
                pending.append((gh_json, gh_txt))
    _prune_github_orphans(normalized, cache_dir, verbosity)
    return pending


def _normalize_repo(repo: str) -> str:
    """`owner/repo` as-is; a full URL → its last two path segments."""
    if repo.startswith(("http://", "https://")):
        return "/".join(repo.rstrip("/").split("/")[-2:])
    return repo


def _remove_quietly(path: str) -> None:
    """Best-effort file removal; a missing file is fine."""
    try:
        os.remove(path)
    except OSError:
        pass


def _prune_github_orphans(
    repos: "List[str]", cache_dir: str, verbose: Verbosity
) -> None:
    """Remove archive JSONs, raw dumps, and per-issue / per-PR dirs for repos
    this corpus no longer tracks. Keyed off the tracked `repos` set (already
    URL-normalised). An empty `repos` is a valid "track nothing": persisted
    repos survive config.merge and a throttled discovery never clears them,
    so an empty set means genuinely none (see autotrack_github), not an
    outage that would make this delete live data.
    """
    keep_json = {
        os.path.basename(paths.github_archive_path(cache_dir, r)) for r in repos
    }
    keep_raw = {
        os.path.basename(paths.raw_github_text_path(cache_dir, r)) for r in repos
    }
    keep_issue_dir = {
        os.path.basename(paths.issue_repo_dir(cache_dir, r)) for r in repos
    }
    keep_pull_dir = {os.path.basename(paths.pull_repo_dir(cache_dir, r)) for r in repos}
    removed = 0
    gh_dir = paths.github_dir(cache_dir)
    if os.path.isdir(gh_dir):
        for name in os.listdir(gh_dir):
            if name.endswith(".json") and name not in keep_json:
                _remove_quietly(os.path.join(gh_dir, name))
                removed += 1
    raw = paths.raw_dir(cache_dir)
    if os.path.isdir(raw):
        for name in os.listdir(raw):
            if (
                name.startswith("github-")
                and name.endswith(".txt")
                and name not in keep_raw
            ):
                _remove_quietly(os.path.join(raw, name))
                removed += 1
    for root, keep_dirs in (
        (paths.issues_dir(cache_dir), keep_issue_dir),
        (paths.pulls_dir(cache_dir), keep_pull_dir),
    ):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            sub = os.path.join(root, name)
            if os.path.isdir(sub) and name not in keep_dirs:
                shutil.rmtree(sub, ignore_errors=True)
                removed += 1
    if removed:
        log(
            f"Pruned {removed} orphaned GitHub artifact(s) for untracked repos.",
            verbose,
            level=LogLevel.STATUS,
        )


#: Consecutive comment-fetch failures after which a repo is given up on for
#: this gather: a host that is down fails every issue the same way, slowly.
_MAX_CONSECUTIVE_COMMENT_FAILURES = 3


class _CommentsUnavailable(Exception):
    """An issue's comments could not be fetched completely (a non-200 page or a
    transport error). `rate_limited` means the whole repo is throttled, so
    asking for other issues would only be refused too."""

    def __init__(self, reason: str, rate_limited: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rate_limited = rate_limited


@dataclass
class _StaleReport:
    """Issues whose comments could not be fetched this gather, and why. Each is
    left at its previously cached copy (or omitted if there is none) rather
    than written with a truncated comment list."""

    stale: List[Any] = field(default_factory=list)
    omitted: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)
    halted: Optional[str] = None

    def add(self, number: Any, reason: str, kept: bool) -> None:
        self.stale.append(number)
        if not kept:
            self.omitted += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def summary(self, repo: str) -> str:
        why = "; ".join(
            f"{reason} x{count}" if count > 1 else reason
            for reason, count in self.reasons.items()
        )
        kept = len(self.stale) - self.omitted
        detail = f"{kept} kept at their cached copy"
        if self.omitted:
            detail += f", {self.omitted} never cached so skipped"
        text = (
            f"GitHub {repo}: {len(self.stale)} issue(s) not refreshed ({detail}); "
            f"comments could not be fetched: {why}"
        )
        if self.halted:
            text += f"; stopped fetching this repo ({self.halted})"
        return text + "; they will be retried on the next gather."


def _comments_failure(res: Any) -> _CommentsUnavailable:
    """Classify a non-200 comments response; a throttle names its Retry-After."""
    status = res.status_code
    headers = getattr(res, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    limited = status == 429 or (
        status == 403
        and (retry_after is not None or headers.get("X-RateLimit-Remaining") == "0")
    )
    if not limited:
        return _CommentsUnavailable(f"HTTP {status}")
    reason = f"rate limited (HTTP {status}"
    if retry_after is not None:
        reason += f", Retry-After {retry_after}s"
        try:
            if float(retry_after) > RETRY_AFTER_MAX:
                reason += f", over the {RETRY_AFTER_MAX}s limit"
        except ValueError:
            pass
    return _CommentsUnavailable(reason + ")", rate_limited=True)


def _fetch_all_issues(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    owner: str,
    repo_name: str,
    headers: Dict[str, str],
    verbose: Verbosity,
    previous: Optional[Dict[Any, Dict[str, Any]]] = None,
    report: Optional[_StaleReport] = None,
) -> List[Dict[str, Any]]:
    """Fetch all issues and their comments from GitHub API.

    An issue whose comments cannot be fetched completely is not written with a
    partial list: its `previous` cached copy is kept (an issue never cached is
    omitted) and it is recorded in `report`; the other issues carry on. A
    throttle on the repo, or repeated failures, stop the fetch there, keeping
    the cached copy of every issue not yet reached.
    """
    previous = previous or {}
    report = report if report is not None else _StaleReport()
    all_issues: List[Dict[str, Any]] = []
    seen: "set[Any]" = set()
    failures = 0
    page = 1
    while report.halted is None:
        api_url = (
            f"https://api.github.com/repos/{owner}/{repo_name}/issues"
            f"?state=all&page={page}&per_page=100"
        )
        res = governed_get(api_url, headers=headers, timeout=60)
        res.raise_for_status()
        issues = res.json()
        if not issues:
            break

        for issue in issues:
            # GitHub API returns both issues and PRs (PRs have a 'pull_request' key)
            if "pull_request" in issue:
                continue

            number = issue.get("number")
            issue_data = {
                "number": number,
                "title": issue.get("title"),
                "state": issue.get("state"),
                "author": issue.get("user", {}).get("login"),
                "createdAt": issue.get("created_at"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "body": issue.get("body"),
                "comments": [],
            }
            if issue.get("comments", 0) > 0:
                if report.halted is not None:
                    # Not asked for: the repo is throttled or down.
                    report.add(number, report.halted, number in previous)
                    if number in previous:
                        all_issues.append(previous[number])
                        seen.add(number)
                    continue
                try:
                    issue_data["comments"] = _fetch_issue_comments(
                        issue.get("comments_url"), headers
                    )
                    failures = 0
                except _CommentsUnavailable as err:
                    failures += 1
                    report.add(number, err.reason, number in previous)
                    if err.rate_limited:
                        report.halted = err.reason
                    elif failures >= _MAX_CONSECUTIVE_COMMENT_FAILURES:
                        report.halted = f"{failures} failures in a row"
                    # Keep the cached copy; never the truncated one.
                    if number in previous:
                        all_issues.append(previous[number])
                        seen.add(number)
                    continue

            all_issues.append(issue_data)
            seen.add(number)

        page += 1
        if len(issues) < 100:
            break
    if report.halted is not None:
        # Issues beyond the point we stopped keep their cached copies.
        for number, cached in previous.items():
            if number not in seen:
                all_issues.append(cached)
                report.add(number, report.halted, True)
    return all_issues


def _fetch_issue_comments(
    comments_url: str, headers: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Fetch every comment for a specific issue, paging the API.

    GitHub's issue-comments endpoint defaults to 30 per page; without paging,
    an active issue's later comments were silently dropped — including the
    actual closing comment that downstream rendering uses as the resolution.
    Raises `_CommentsUnavailable` if any page is not a 200 or the request
    fails: a truncated list must not be mistaken for the whole discussion.
    """
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        try:
            c_res = governed_get(
                comments_url,
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=30,
            )
        except requests.RequestException as err:
            raise _CommentsUnavailable(f"{type(err).__name__}") from err
        if c_res.status_code != 200:
            raise _comments_failure(c_res)
        comments = c_res.json()
        if not comments:
            break
        out.extend(
            {
                "author": comment.get("user", {}).get("login"),
                "createdAt": comment.get("created_at"),
                "body": comment.get("body"),
            }
            for comment in comments
        )
        if len(comments) < 100:
            break
        page += 1
    return out
