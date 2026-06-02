"""Shared fixtures and helpers for the ietf-llm test suite.

The tool reads/writes under ~/.cache/ietf-llm/ and ~/.config/ietf-llm/.
Tests must never touch the real user directories, so the `isolated_home`
fixture monkeypatches $HOME (and $USERPROFILE on Windows) to a tmp dir
for the whole test. `os.path.expanduser("~")` honours these, so every
call to `get_cache_dir()` / `get_config_dir()` lands inside the sandbox.
"""

from __future__ import annotations

import email.message
import email.policy
import email.utils
import json
import os
from pathlib import Path
from typing import Any, Iterable

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox $HOME for the duration of one test.

    Anything the code does with ~/.cache or ~/.config lands under
    tmp_path/.cache or tmp_path/.config and is cleaned up by pytest.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # An ambient IETF_LLM_CACHE_DIR / _CONFIG_DIR would point the code at a
    # real corpus and defeat the HOME sandbox -- clear them so tests stay
    # isolated regardless of the developer's environment.
    monkeypatch.delenv("IETF_LLM_CACHE_DIR", raising=False)
    monkeypatch.delenv("IETF_LLM_CONFIG_DIR", raising=False)
    monkeypatch.delenv("IETF_LLM_INDEX_DIR", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_datatracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Datatracker-touching code paths in every test.

    The real calls hit https://datatracker.ietf.org/ — fine in production
    but a CI-fragility nightmare. Tests that want to exercise these
    surfaces should monkeypatch this fixture out, or directly invoke
    the relevant Registry method (for roles) or stub `_get_json` in
    `datatracker_history` (for governance events).
    """
    from ietf_llm import people  # pylint: disable=import-outside-toplevel
    from ietf_llm.gather import (  # pylint: disable=import-outside-toplevel
        ballots,
        datatracker_github,
        datatracker_history,
        github_users,
    )

    def _noop(wg: str, registry: object, verbose: object) -> None:  # noqa: ARG001
        return

    monkeypatch.setattr(people, "_ingest_datatracker_roles", _noop)
    # Block the GitHub-username profile-resource lookups (the new linking
    # pass) at the network boundary. With `_get_json` returning None the
    # index build aborts and the pass is a no-op. Tests exercising the
    # link path monkeypatch this attribute with canned responses.
    monkeypatch.setattr(
        datatracker_github, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
    )
    # Block HTTP calls from datatracker_history at the network boundary;
    # the real fetch_* functions still run, they just receive None and
    # return []. Tests that want to exercise the parsing path
    # monkeypatch this same attribute to supply canned responses.
    monkeypatch.setattr(
        datatracker_history, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
    )
    # Same treatment for the ballot fetcher — the timeline build calls
    # into it now, and we don't want any test that builds a timeline
    # to hit datatracker.ietf.org. Tests exercising ballot parsing
    # monkeypatch this attribute to supply canned ballot responses.
    monkeypatch.setattr(
        ballots, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
    )
    # Block GitHub user lookups too — tests that want to exercise the
    # name-resolution path stub `_fetch_one` (or `resolve_logins`)
    # directly. Default: act as if every login is uncacheable
    # transient-failure, so the registry's canonical_name stays unchanged.
    monkeypatch.setattr(
        github_users, "_fetch_one",
        lambda login, headers, verbose: github_users._Outcome(  # noqa: ARG005
            name=None, cacheable=False,
        ),
    )


# --- Helpers for building synthetic corpus files ---------------------------


def write_eml(
    cache_root: Path,
    wg: str,
    list_name: str,
    uid: int,
    subject: str,
    from_addr: str,
    date: str,
    body: str = "body text",
) -> Path:
    """Create one .eml file in the IMAP cache layout that mbox.py uses:

        <cache_root>/.cache/ietf-llm/imap-cache/<wg>/<list_name>/<uid>.eml
    """
    imap_dir = (
        cache_root / ".cache" / "ietf-llm" / "imap-cache" / wg / list_name
    )
    imap_dir.mkdir(parents=True, exist_ok=True)
    msg = email.message.EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["Date"] = date
    msg.set_content(body)
    path = imap_dir / f"{uid}.eml"
    path.write_bytes(bytes(msg))
    return path


def write_github_archive(
    cache_root: Path,
    wg: str,
    repo: str,
    issues: Iterable[dict[str, Any]],
) -> Path:
    """Create a GitHub archive JSON in the post-reorg cache layout:

        <cache_root>/.cache/ietf-llm/<wg>/files/github/<slug>.json
    """
    archives_dir = (
        cache_root / ".cache" / "ietf-llm" / wg / "files" / "github"
    )
    archives_dir.mkdir(parents=True, exist_ok=True)
    slug = repo.replace("/", "-").lower()
    path = archives_dir / f"{slug}.json"
    path.write_text(
        json.dumps(
            {
                "repo": repo,
                "timestamp": "2026-05-26T00:00:00Z",
                "issues": list(issues),
            }
        )
    )
    return path


def write_cache_file(
    cache_root: Path, wg: str, name: str, content: str = "x"
) -> Path:
    """Drop a file into <cache_root>/.cache/ietf-llm/<wg>/files/<name>.

    `name` can include subdirectories (e.g. `digests/issues.md`); any
    parent directories needed for the target path are created.
    """
    files_dir = cache_root / ".cache" / "ietf-llm" / wg / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    path = files_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_issue(
    number: int,
    title: str,
    state: str = "open",
    labels: list[str] | None = None,
    author: str = "alice",
    updated_at: str = "2026-05-01T00:00:00Z",
    body: str = "issue body",
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an issue dict matching the archive.json schema."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "author": author,
        "createdAt": updated_at,
        "updatedAt": updated_at,
        "labels": labels or [],
        "body": body,
        "comments": comments or [],
    }
