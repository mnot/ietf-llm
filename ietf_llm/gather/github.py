import json
import os
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import requests

from ..utils import (
    DEFAULT_HEADERS,
    LogLevel,
    Verbosity,
    atomic_open,
    governed_get,
    log,
)


def iter_issue_archives(archives_dir: str) -> "Iterator[Dict[str, Any]]":
    """Yield each parsed `<repo>.json` issue archive under `archives_dir`.

    Skips non-JSON files and any archive that fails to read / parse.
    Shared by the people registry and the timeline builder so they
    don't each re-implement the walk-and-load loop.
    """
    if not os.path.isdir(archives_dir):
        return
    for name in sorted(os.listdir(archives_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(archives_dir, name), "r", encoding="utf-8") as fh:
                yield json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue


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
    except (json.JSONDecodeError, OSError) as err:
        log(f"Error parsing GitHub JSON: {err}", verbose, level=LogLevel.ERROR)
        return []

    issues = data.get("issues", [])
    repo_name = data.get("repo", "Unknown Repo")

    processed_count = 0
    filtered_count = 0

    with open(output_file, "w", encoding="utf-8") as out_fh:
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
    """Download GitHub issues JSON using the API from 'owner/repo' short name."""
    if repo_short.startswith(("http://", "https://")):
        log(
            f"Direct downloading GitHub issues from {repo_short}...",
            verbose,
            level=LogLevel.STATUS,
        )
        try:
            response = governed_get(repo_short, headers=DEFAULT_HEADERS, timeout=60)
            response.raise_for_status()
            with atomic_open(dest_path) as json_file:
                json_file.write(response.text)
            return True
        except (requests.RequestException, OSError) as err:
            log(
                f"Error downloading GitHub issues: {err}", verbose, level=LogLevel.ERROR
            )
            return False

    # Expecting owner/repo
    if "/" not in repo_short:
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
                elif "issues" not in archive_data:
                    # If it's a dict but missing 'issues', we might still want to wrap it
                    # or handle it differently. For now, assume it might be a single issue
                    # or some other format and wrap if it's not our expected schema.
                    archive_data = {
                        "repo": f"{owner}/{repo}",
                        "timestamp": datetime.now().isoformat(),
                        "issues": [archive_data],
                    }
                with atomic_open(dest_path) as json_fh:
                    json.dump(archive_data, json_fh, indent=2)
                return True
            except (json.JSONDecodeError, TypeError) as err:
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

    try:
        all_issues = _fetch_all_issues(owner, repo, headers, verbose)
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


def _fetch_all_issues(
    owner: str, repo_name: str, headers: Dict[str, str], verbose: Verbosity
) -> List[Dict[str, Any]]:
    """Fetch all issues and their comments from GitHub API."""
    all_issues = []
    page = 1
    while True:
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

            issue_data = {
                "number": issue.get("number"),
                "title": issue.get("title"),
                "state": issue.get("state"),
                "author": issue.get("user", {}).get("login"),
                "createdAt": issue.get("created_at"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "body": issue.get("body"),
                "comments": [],
            }
            if issue.get("comments", 0) > 0:
                issue_data["comments"] = _fetch_issue_comments(
                    issue.get("comments_url"), headers
                )

            all_issues.append(issue_data)

        page += 1
        if len(issues) < 100:
            break
    return all_issues


def _fetch_issue_comments(
    comments_url: str, headers: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Fetch every comment for a specific issue, paging the API.

    GitHub's issue-comments endpoint defaults to 30 per page; without paging,
    an active issue's later comments were silently dropped — including the
    actual closing comment that downstream rendering uses as the resolution.
    """
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        c_res = governed_get(
            comments_url,
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        if c_res.status_code != 200:
            break
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
