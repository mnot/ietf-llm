"""Tests for orphan pruning in the GitHub gather stage.

`gather/github.py:_prune_github_orphans` sweeps archive JSONs, raw dumps,
and per-issue dirs for repos a corpus no longer tracks — the guard that
keeps a dropped (or leaked) repo's issues from lingering in the cache and
surfacing as if still tracked. Symmetric with the thread / ballot stages.
"""

from __future__ import annotations

import json
from pathlib import Path

from ietf_llm.gather.github import _prune_github_orphans
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir


def _seed_repo(cache: Path, repo: str) -> None:
    slug = repo.replace("/", "-").lower()
    gh = cache / "github"
    gh.mkdir(parents=True, exist_ok=True)
    (gh / f"{slug}.json").write_text(json.dumps({"repo": repo, "issues": []}))
    issdir = cache / "issues" / slug
    issdir.mkdir(parents=True, exist_ok=True)
    (issdir / "1.md").write_text("# issue 1\n")
    raw = cache / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"github-{slug}.txt").write_text("dump\n")


def _exists(cache: Path, repo: str) -> bool:
    slug = repo.replace("/", "-").lower()
    return (
        (cache / "github" / f"{slug}.json").exists()
        or (cache / "issues" / slug).exists()
        or (cache / "raw" / f"github-{slug}.txt").exists()
    )


def test_prune_removes_untracked_keeps_tracked(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("tls"))
    _seed_repo(cache, "tlswg/keep")
    _seed_repo(cache, "ietf-wg-aipref/drafts")  # orphan from a leaked gather

    _prune_github_orphans(["tlswg/keep"], str(cache), Verbosity.QUIET)

    assert _exists(cache, "tlswg/keep")
    assert not _exists(cache, "ietf-wg-aipref/drafts")


def test_prune_empty_repos_clears_everything(isolated_home: Path) -> None:
    # Empty set is a valid "track nothing" (e.g. httpbis with throttled
    # discovery and nothing persisted) — every github artifact is orphaned.
    cache = Path(get_wg_file_cache_dir("httpbis"))
    _seed_repo(cache, "ietf-wg-aipref/drafts")

    _prune_github_orphans([], str(cache), Verbosity.QUIET)

    assert not _exists(cache, "ietf-wg-aipref/drafts")


def test_prune_noop_when_no_github_dir(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("rswg"))
    _prune_github_orphans([], str(cache), Verbosity.QUIET)  # must not raise
    assert not (cache / "github").exists()
