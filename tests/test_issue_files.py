"""Tests for per-issue Markdown files (symmetric with per-thread mail files).

`gather/issue_files.py` writes one `<wg>-issue-<repo-slug>-<NNN>.md` per
GitHub issue, with frontmatter + outline + per-comment sections. These
tests cover:

- file shape (header, outline, description, comments)
- canonical-name resolution via the people Registry
- participant extraction (author + every commenter, deduped)
- stale-file clearing on re-write
- skipped/malformed archives don't crash
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.gather.issue_files import (
    _normalise_html,
    _participants,
    issue_slug,
    write_issue_files,
)
from ietf_llm.people import Registry
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import make_issue, write_github_archive


def _issue_text(wg: str, repo: str, number: int) -> str:
    cache = Path(get_wg_file_cache_dir(wg))
    return (cache / f"{wg}-issue-{issue_slug(repo, number)}.md").read_text()


# --- pure helpers ---------------------------------------------------------


def test_issue_slug_lowercases_and_dashes() -> None:
    assert issue_slug("Org/Repo", 42) == "org-repo-42"


def test_participants_dedupes_and_orders() -> None:
    issue = {
        "author": "alice",
        "comments": [
            {"author": "bob"},
            {"author": "alice"},  # duplicate of author
            {"author": "carol"},
            {"author": "bob"},  # duplicate
        ],
    }
    assert _participants(issue, None) == ["alice", "bob", "carol"]


def test_participants_uses_registry_canonical_names() -> None:
    registry = Registry()
    p = registry.add_github_author("alice")
    assert p is not None
    p.canonical_name = "Alice Wonderland"
    p = registry.add_github_author("bob")
    assert p is not None
    p.canonical_name = "Bob Builder"
    issue = {"author": "alice", "comments": [{"author": "bob"}]}
    assert _participants(issue, registry) == ["Alice Wonderland", "Bob Builder"]


def _register_canonical(registry: Registry, login: str, name: str) -> None:
    p = registry.add_github_author(login)
    assert p is not None
    p.canonical_name = name


# --- write_issue_files end-to-end -----------------------------------------


def test_write_issue_files_creates_one_per_issue(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(1, "First", state="open"),
            make_issue(2, "Second", state="closed"),
        ],
    )
    cache = get_wg_file_cache_dir("wg")
    written = write_issue_files("wg", cache, verbose=Verbosity.QUIET)
    assert len(written) == 2
    assert (Path(cache) / "wg-issue-org-repo-1.md").exists()
    assert (Path(cache) / "wg-issue-org-repo-2.md").exists()


def test_issue_file_has_expected_structure(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                7,
                "Vocab debate",
                state="open",
                labels=["vocab", "discuss"],
                author="alice",
                updated_at="2026-04-14T10:00:00Z",
                body="Initial problem statement.",
                comments=[
                    {
                        "author": "bob",
                        "createdAt": "2026-04-15T11:30:00Z",
                        "body": "Counter-argument.",
                    },
                    {
                        "author": "carol",
                        "createdAt": "2026-04-16T09:00:00Z",
                        "body": "Synthesis.",
                    },
                ],
            ),
        ],
    )
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)
    text = _issue_text("wg", "org/repo", 7)
    assert "# Issue #7: Vocab debate" in text
    assert "**State:** OPEN" in text
    assert "**Opened by:** alice on 2026-04-14 10:00" in text
    assert "**Labels:** vocab, discuss" in text
    assert "**Comments:** 2" in text
    assert "**Participants (3):** alice, bob, carol" in text
    # Outline must list every event with section indices.
    assert "## Outline" in text
    assert "**[1]** 2026-04-14 10:00 — alice _(opened issue)_" in text
    assert "**[2]** 2026-04-15 11:30 — bob" in text
    assert "**[3]** 2026-04-16 09:00 — carol" in text
    # Per-section bodies share the thread file's `### [N] DATE — Author` shape
    # so chunking can reuse the thread chunker.
    assert "### [1] 2026-04-14 10:00 — alice _(opened issue)_" in text
    assert "### [2] 2026-04-15 11:30 — bob" in text
    assert "Counter-argument." in text
    assert "Synthesis." in text


def test_issue_file_uses_canonical_names(isolated_home: Path) -> None:
    registry = Registry()
    _register_canonical(registry, "alice", "Alice Wonderland")
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [make_issue(1, "T", author="alice")],
    )
    write_issue_files(
        "wg",
        get_wg_file_cache_dir("wg"),
        registry=registry,
        verbose=Verbosity.QUIET,
    )
    text = _issue_text("wg", "org/repo", 1)
    assert "Alice Wonderland" in text
    # The raw login shouldn't leak when the registry can canonicalise it.
    assert "Opened by:** alice " not in text


def test_empty_body_and_empty_comments_render_placeholder(
    isolated_home: Path,
) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                1,
                "T",
                body="",
                comments=[{"author": "bob", "createdAt": "2026-04-15T11:30:00Z", "body": ""}],
            ),
        ],
    )
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)
    text = _issue_text("wg", "org/repo", 1)
    assert "_(no description provided)_" in text
    assert "_(empty comment)_" in text


def test_stale_issue_files_are_wiped(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("wg"))
    cache.mkdir(parents=True, exist_ok=True)
    # An old file that no longer corresponds to any current issue.
    stale = cache / "wg-issue-org-repo-999.md"
    stale.write_text("stale content")
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [make_issue(1, "T")],
    )
    write_issue_files("wg", str(cache), verbose=Verbosity.QUIET)
    assert not stale.exists()
    assert (cache / "wg-issue-org-repo-1.md").exists()


def test_malformed_json_is_skipped(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("wg"))
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "wg-github-bad-repo.json").write_text("{not json")
    # Should not raise.
    written = write_issue_files("wg", str(cache), verbose=Verbosity.QUIET)
    assert written == []


def test_no_cache_dir_returns_empty(tmp_path: Path) -> None:
    assert write_issue_files("wg", str(tmp_path / "nope")) == []


# --- HTML normalisation in issue bodies -----------------------------------


def test_normalise_html_passes_through_when_no_tags() -> None:
    assert _normalise_html("plain prose") == "plain prose"
    assert _normalise_html("") == ""


def test_normalise_html_unrolls_ul_li() -> None:
    # The literal consumer-feedback case: a `<ul><li>...</li>` list
    # embedded in a table cell.
    raw = "Pros: <ul><li>fast</li><li>easy</li><li>clear</li></ul>"
    out = _normalise_html(raw)
    assert "<li>" not in out and "</li>" not in out
    assert "<ul>" not in out and "</ul>" not in out
    # Each bullet on its own line, so the list-aware snippet detector
    # can find ≥3 items.
    assert "- fast" in out
    assert "- easy" in out
    assert "- clear" in out


def test_normalise_html_unrolls_ol_li() -> None:
    out = _normalise_html("<ol><li>one</li><li>two</li></ol>")
    assert "<ol>" not in out and "<li>" not in out
    assert "- one" in out
    assert "- two" in out


def test_normalise_html_replaces_br_with_newline() -> None:
    out = _normalise_html("first<br>second<br/>third<br />fourth")
    # Three breaks → four lines worth of content.
    assert out.count("\n") >= 3
    assert "<br" not in out.lower()


def test_normalise_html_leaves_unknown_tags_alone() -> None:
    # Better to leave a tag visible than to misrender something we
    # haven't tested. <pre> blocks, in particular, should not be
    # silently mangled.
    raw = "<pre>code block</pre> <a href='x'>link</a>"
    out = _normalise_html(raw)
    assert "<pre>" in out
    assert "<a href" in out


def test_normalise_html_handles_uppercase_tags() -> None:
    out = _normalise_html("<UL><LI>x</LI><LI>y</LI></UL>")
    assert "- x" in out
    assert "- y" in out


def test_issue_file_renders_html_lists_as_markdown(isolated_home: Path) -> None:
    # End-to-end: the issue body has an HTML list; the written file
    # has it normalised so the list-aware snippet path can engage.
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                1, "Has HTML body",
                body="Options: <ul><li>fast</li><li>easy</li><li>clear</li></ul>",
            ),
        ],
    )
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)
    text = _issue_text("wg", "org/repo", 1)
    assert "<ul>" not in text
    assert "<li>" not in text
    assert "- fast" in text
