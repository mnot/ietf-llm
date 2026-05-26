"""Tests that the GitHub issues .txt file canonicalises author names
when a Registry is supplied."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.gather.github import process_github_issues
from ietf_llm.people import Registry

from conftest import write_github_archive, make_issue


def test_github_txt_uses_canonical_names_with_registry(
    isolated_home: Path, tmp_path: Path,
) -> None:
    archive = write_github_archive(
        isolated_home, "wg", "org/repo",
        [
            make_issue(
                1, "Cookie partitioning", state="open", author="mnot",
                comments=[
                    {
                        "author": "mnot",
                        "createdAt": "2025-01-02T00:00:00Z",
                        "body": "follow up",
                    },
                ],
            ),
        ],
    )
    # Wire the registry so `mnot` resolves to Mark.
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_github_author("mnot")

    out_path = tmp_path / "wg-github-org-repo.txt"
    process_github_issues(
        str(archive), str(out_path), registry=r,
    )
    text = out_path.read_text()
    assert "Author: Mark Nottingham" in text
    assert "Comment by Mark Nottingham" in text
    # The raw login should not appear in those lines.
    assert "Author: mnot" not in text


def test_github_txt_falls_back_without_registry(
    isolated_home: Path, tmp_path: Path,
) -> None:
    archive = write_github_archive(
        isolated_home, "wg", "org/repo",
        [make_issue(1, "Title", author="mnot")],
    )
    out_path = tmp_path / "wg-github-org-repo.txt"
    process_github_issues(str(archive), str(out_path))
    text = out_path.read_text()
    # No registry → raw login stays.
    assert "Author: mnot" in text
