"""Tests for per-issue Markdown files (symmetric with per-thread mail files).

`gather/sources/issue_files.py` writes one `<wg>-issue-<repo-slug>-<NNN>.md` per
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

from ietf_llm.gather.sources.issue_files import (
    _closing_rationale,
    _detect_duplicate_of,
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
    repo_slug = repo.replace("/", "-").lower()
    return (cache / "issues" / repo_slug / f"{number}.md").read_text()


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
    assert (Path(cache) / "issues" / "org-repo" / "1.md").exists()
    assert (Path(cache) / "issues" / "org-repo" / "2.md").exists()


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
    repo_subdir = cache / "issues" / "org-repo"
    repo_subdir.mkdir(parents=True, exist_ok=True)
    # An old file that no longer corresponds to any current issue.
    stale = repo_subdir / "999.md"
    stale.write_text("stale content")
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [make_issue(1, "T")],
    )
    write_issue_files("wg", str(cache), verbose=Verbosity.QUIET)
    assert not stale.exists()
    assert (repo_subdir / "1.md").exists()


def test_malformed_json_is_skipped(isolated_home: Path) -> None:
    cache = Path(get_wg_file_cache_dir("wg"))
    archives = cache / "github"
    archives.mkdir(parents=True, exist_ok=True)
    (archives / "bad-repo.json").write_text("{not json")
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


def test_issue_file_emits_github_url(isolated_home: Path) -> None:
    # Consumer feedback: file paths in search results aren't citeable;
    # the GitHub URL is reconstructible from repo + number and should
    # be emitted into the issue file frontmatter once at render time
    # so consumers (LLM or human) don't have to.
    write_github_archive(
        isolated_home,
        "wg",
        "ietf-wg-aipref/drafts",
        [make_issue(155, "Category for RAG")],
    )
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)
    text = _issue_text("wg", "ietf-wg-aipref/drafts", 155)
    assert (
        "**URL:** https://github.com/ietf-wg-aipref/drafts/issues/155"
        in text
    )


def test_issue_file_skips_url_when_repo_lacks_slash(
    isolated_home: Path,
) -> None:
    # Edge case: if the archive's "repo" field is malformed (no owner/
    # repo split), we'd produce a bogus URL. Skip the line entirely
    # rather than emit garbage.
    write_github_archive(isolated_home, "wg", "bare-repo-name", [make_issue(1, "T")])
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)
    text = _issue_text("wg", "bare-repo-name", 1)
    assert "**URL:**" not in text


def test_issue_file_decorates_author_with_role(isolated_home: Path) -> None:
    # Same role-attribution story as for thread files: section headers
    # for issue comments should include the author's role tag when one
    # is known, so an LLM ranking arguments sees the weight.
    registry = Registry()
    registry.add_email_message(
        "Mark Nottingham <mnot@mnot.net>", None,
    )
    p = registry.add_github_author("mnot")
    assert p is not None
    p.canonical_name = "Mark Nottingham"
    registry.add_datatracker_role(
        "Mark Nottingham", "mnot@mnot.net", "Chair",
    )
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                1, "T", author="mnot",
                comments=[
                    {
                        "author": "mnot",
                        "createdAt": "2026-04-15T11:30:00Z",
                        "body": "Decision.",
                    },
                ],
            ),
        ],
    )
    write_issue_files(
        "wg",
        get_wg_file_cache_dir("wg"),
        registry=registry,
        verbose=Verbosity.QUIET,
    )
    text = _issue_text("wg", "org/repo", 1)
    # "Opened by:" line, outline bullet, description section header,
    # comment section header — all four should carry "(Chair)".
    assert "Opened by:** Mark Nottingham (Chair)" in text
    assert "— Mark Nottingham (Chair) _(opened issue)_" in text
    assert "### [2] 2026-04-15 11:30 — Mark Nottingham (Chair)" in text


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


# --- Duplicate-of detection (consumer feedback) ---------------------------


def test_detect_duplicate_of_picks_up_canonical_phrasing() -> None:
    # "This appears to be a duplicate of: #155" — the literal phrasing
    # the consumer cited from TimidRobot's comment on issue #169.
    issue = {
        "number": 169,
        "body": "",
        "comments": [
            {"author": "x", "body": "This appears to be a duplicate of: #155"},
        ],
    }
    assert _detect_duplicate_of(issue) == 155


def test_detect_duplicate_of_handles_variants() -> None:
    for body, expected in [
        ("Duplicate of #42", 42),
        ("Closing as dup of #7", 7),
        ("This is a dupe of #1234", 1234),
        ("duplicate 99", 99),  # bare number without #
    ]:
        issue = {"number": 999, "body": body, "comments": []}
        assert _detect_duplicate_of(issue) == expected, body


def test_detect_duplicate_of_ignores_self_reference() -> None:
    # Don't flag an issue as a duplicate of itself even if the body
    # says so (it shouldn't, but defensively).
    issue = {
        "number": 155,
        "body": "Marked as duplicate of #155.",
        "comments": [],
    }
    assert _detect_duplicate_of(issue) is None


def test_detect_duplicate_of_returns_none_for_unrelated_text() -> None:
    issue = {
        "number": 1,
        "body": "Discussion of duplicate detection algorithms.",
        "comments": [],
    }
    assert _detect_duplicate_of(issue) is None


# --- Closing rationale extraction -----------------------------------------


def test_closing_rationale_for_open_issue_is_none() -> None:
    # Open issues have no rationale (nothing has been settled).
    issue = {
        "state": "OPEN",
        "comments": [
            {"author": "x", "body": "ongoing discussion",
             "createdAt": "2026-01-01T00:00:00Z"},
        ],
    }
    assert _closing_rationale(issue, None) is None


def test_closing_rationale_uses_last_comment_when_closed() -> None:
    # Last comment becomes the rationale. We don't try to detect chair
    # authorship explicitly — role tags make that visible already.
    issue = {
        "state": "CLOSED",
        "comments": [
            {"author": "x", "body": "early",
             "createdAt": "2026-01-01T00:00:00Z"},
            {"author": "y", "body": "Removed from the next drafts.",
             "createdAt": "2026-02-01T10:00:00Z"},
        ],
    }
    out = _closing_rationale(issue, None)
    assert out is not None
    assert "Removed from the next drafts." in out
    # Quoted as a blockquote so it's visually distinct in the file.
    assert "> Removed" in out
    assert "2026-02-01 10:00" in out


def test_closing_rationale_truncates_very_long_comment() -> None:
    long_body = "very long comment " * 100  # ~1800 chars
    issue = {
        "state": "CLOSED",
        "comments": [
            {"author": "x", "body": long_body,
             "createdAt": "2026-02-01T10:00:00Z"},
        ],
    }
    out = _closing_rationale(issue, None)
    assert out is not None
    # Truncated with ellipsis somewhere in the body region.
    assert "..." in out
    # And the whole formatted block stays well under the original.
    assert len(out) < 600


def test_closing_rationale_handles_no_comments() -> None:
    issue = {"state": "CLOSED", "comments": []}
    assert _closing_rationale(issue, None) is None


# --- end-to-end: rendered file carries the new metadata ------------------


def test_issue_file_includes_duplicate_of_and_rationale(
    isolated_home: Path,
) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(
                155,
                "Vocab decision",
                state="CLOSED",
                updated_at="2026-02-01T10:00:00Z",
                body="Original question.",
                comments=[
                    {"author": "alice", "body": "Early discussion.",
                     "createdAt": "2026-01-15T10:00:00Z"},
                    {"author": "mnot",
                     "body": "Removed from the next drafts.",
                     "createdAt": "2026-02-01T10:00:00Z"},
                ],
            ),
            make_issue(
                169,
                "Vocab redux",
                state="CLOSED",
                body="See discussion in #155.",
                comments=[
                    {"author": "timidrobot",
                     "body": "This appears to be a duplicate of: #155",
                     "createdAt": "2026-02-02T09:00:00Z"},
                ],
            ),
        ],
    )
    write_issue_files("wg", get_wg_file_cache_dir("wg"), verbose=Verbosity.QUIET)

    text_155 = _issue_text("wg", "org/repo", 155)
    assert "**Closing rationale:**" in text_155
    assert "Removed from the next drafts." in text_155
    # Issue 155 has no duplicate marker.
    assert "**Duplicate of:**" not in text_155

    text_169 = _issue_text("wg", "org/repo", 169)
    assert "**Duplicate of:** #155" in text_169
    # 169 is closed too, so it has BOTH duplicate-of AND rationale.
    assert "**Closing rationale:**" in text_169
