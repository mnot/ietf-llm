import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
import requests
from .utils import LogLevel, Verbosity, log


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
) -> List[str]:
    """Process a GitHub issues JSON archive and write cleaned text to output_file."""
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
            issue_labels = issue.get("labels", [])
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
            out_fh.write(f"Author: {author}\n")
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

                    out_fh.write(f"--- Comment by {c_author} on {c_date} ---\n")
                    out_fh.write(c_body + "\n\n")

            out_fh.write("=" * 80 + "\n\n")
            processed_count += 1

    msg = f"Done! Extracted {processed_count} issues to {output_file}."
    if filtered_count > 0:
        msg += f" ({filtered_count} issues filtered out by labels)"
    log(msg, verbose, level=LogLevel.STATUS)
    return [output_file]


def download_github_issues(
    repo_short: str,
    dest_path: str,
    token: Optional[str] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> bool:
    """Download GitHub issues JSON using the API from 'owner/repo' short name."""
    if repo_short.startswith("http"):
        log(
            f"Direct downloading GitHub issues from {repo_short}...",
            verbose,
            level=LogLevel.STATUS,
        )
        try:
            response = requests.get(repo_short, timeout=60)
            response.raise_for_status()
            with open(dest_path, "w", encoding="utf-8") as json_file:
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
        response = requests.get(archive_url, timeout=30)
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
                with open(dest_path, "w", encoding="utf-8") as json_fh:
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
    headers = {"Accept": "application/vnd.github.v3+json"}
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
        with open(dest_path, "w", encoding="utf-8") as json_fh:
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
        res = requests.get(api_url, headers=headers, timeout=60)
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
    """Fetch comments for a specific issue."""
    c_res = requests.get(comments_url, headers=headers, timeout=30)
    if c_res.status_code == 200:
        comments = c_res.json()
        return [
            {
                "author": comment.get("user", {}).get("login"),
                "createdAt": comment.get("created_at"),
                "body": comment.get("body"),
            }
            for comment in comments
        ]
    return []
