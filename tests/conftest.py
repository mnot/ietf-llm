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
        datatracker_history,
    )

    def _noop(wg: str, registry: object, verbose: object) -> None:  # noqa: ARG001
        return

    monkeypatch.setattr(people, "_ingest_datatracker_roles", _noop)
    # Block HTTP calls from datatracker_history at the network boundary;
    # the real fetch_* functions still run, they just receive None and
    # return []. Tests that want to exercise the parsing path
    # monkeypatch this same attribute to supply canned responses.
    monkeypatch.setattr(
        datatracker_history, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
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
    """Create a GitHub archive JSON in the files-cache layout:

        <cache_root>/.cache/ietf-llm/<wg>/files/<wg>-github-<slug>.json
    """
    files_dir = cache_root / ".cache" / "ietf-llm" / wg / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    slug = repo.replace("/", "-")
    path = files_dir / f"{wg}-github-{slug}.json"
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
    """Drop a file into <cache_root>/.cache/ietf-llm/<wg>/files/<name>."""
    files_dir = cache_root / ".cache" / "ietf-llm" / wg / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    path = files_dir / name
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
