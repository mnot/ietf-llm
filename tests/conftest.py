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
import time
from pathlib import Path
from typing import Any, Iterable

import pytest


@pytest.fixture(autouse=True)
def _gather_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the gather pipeline in-process during tests, not in a subprocess.

    Production spawns a child so a CPU-heavy stage can't stall the server, but
    the suite drives gather by monkeypatching `run_gather` / the stages, which a
    separate process would not see. The real subprocess path has its own test
    (`test_gather_subprocess.py`), which clears this flag explicitly."""
    monkeypatch.setenv("IETF_LLM_GATHER_INPROCESS", "1")
    # Never reach the real seed store over the network during tests (the baked
    # default IETF_LLM_SEED_URL is a live host). Seed tests opt in by setting it
    # to a local file store; the disable tests set it explicitly.
    monkeypatch.setenv("IETF_LLM_SEED_URL", "off")


@pytest.fixture(autouse=True)
def _reset_seed_catalog() -> None:
    """Clear the seed-catalog in-process refresh throttle before each test — it is
    process-global, so it would otherwise leak across the per-test isolated caches
    and suppress a legitimate refresh."""
    from ietf_llm.seed import catalog  # pylint: disable=import-outside-toplevel

    catalog.reset_state()


@pytest.fixture(autouse=True)
def _drain_gather_worker() -> Iterable[None]:
    """Wait for the shared gather worker to finish any in-flight job before the
    next test runs. The worker is a process-wide daemon, and a job's terminal
    status is written *before* the worker pops it from the registry — so without
    this, a job from one test could still be popping `_jobs` (or releasing its
    lease) as the next test starts, contaminating it."""
    yield
    from ietf_llm.gather import runner as gather_runner  # pylint: disable=import-outside-toplevel

    deadline = time.time() + 5.0
    while time.time() < deadline:
        with gather_runner._registry_lock:  # pylint: disable=protected-access
            idle = not gather_runner._jobs  # pylint: disable=protected-access
        if idle and gather_runner._queue.empty():  # pylint: disable=protected-access
            return
        time.sleep(0.005)


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
    from ietf_llm.gather.sources import (  # pylint: disable=import-outside-toplevel
        ballots,
        datatracker,
        datatracker_github,
        datatracker_history,
        datatracker_people,
        document_authors,
        github_users,
    )

    def _noop(wg: str, registry: object, verbose: object) -> None:  # noqa: ARG001
        return

    monkeypatch.setattr(people, "_ingest_datatracker_roles", _noop)
    # Block the document/state/roles JSON API at the network boundary.
    # get_wg_documents -> draft_state_slugs() / iter_group_documents() both
    # route through this; with it returning None the draft-state map is
    # empty (state recorded as None, drafts still embed) and document
    # listing is empty. Tests exercising these stub iter_group_documents /
    # draft_state_slugs on the drafts module, or this attribute directly.
    monkeypatch.setattr(
        datatracker, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
    )
    # Block the GitHub-username profile-resource lookups (the new linking
    # pass) at the network boundary. With `_get_json` returning None the
    # index build aborts and the pass is a no-op. Tests exercising the
    # link path monkeypatch this attribute with canned responses.
    monkeypatch.setattr(
        datatracker_github, "_get_json",
        lambda path_or_url, timeout=10.0: None,  # noqa: ARG005
    )
    # Block the mail-address → person-id resolver (the mail-reconciliation
    # pass) at the network boundary. With `_get_json` returning None each
    # chunk is treated as a transient failure and the pass is a no-op. Tests
    # exercising it monkeypatch this attribute with canned responses.
    monkeypatch.setattr(
        datatracker_people, "_get_json",
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
    # Block the per-document authorship/affiliation lookups. This one has
    # its own binding to patch: `document_authors` does `from .datatracker
    # import _get_json`, so rebinding the attribute on `datatracker` above
    # leaves its copy pointing at the real function. With it returning
    # None every document falls back to the draft text, which is also what
    # an offline gather does. Tests exercising the authoritative path stub
    # this attribute with canned rows.
    monkeypatch.setattr(
        document_authors, "_get_json",
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
