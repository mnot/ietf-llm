"""End-to-end tests for the GitHub issues digest builder.

Specifically exercises:
- Both lowercase ('open') and uppercase ('OPEN') state values, since the
  archive JSON ships in both forms depending on the exporter.
- Open-first sort order with newest-first within each group.
- Totals line matches the actual state distribution.
- Returns None when there are no github archives in the cache.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest import generate_digests
from ietf_llm.utils import get_wg_file_cache_dir

from conftest import make_issue, write_github_archive


def _digest_text(wg: str) -> str:
    path = Path(get_wg_file_cache_dir(wg)) / f"{wg}-_issues.md"
    return path.read_text()


def test_no_archives_no_digest_written(isolated_home: Path) -> None:
    # A WG with no github JSON archives should not get an issues digest.
    cache = Path(get_wg_file_cache_dir("wg"))
    paths = generate_digests("wg", str(cache), summarize_model=None)
    assert not (cache / "wg-_issues.md").exists()
    assert all("_issues.md" not in p for p in paths)


def test_lowercase_state_counted_correctly(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(1, "Open one", state="open"),
            make_issue(2, "Closed one", state="closed"),
        ],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "_Totals: 1 open, 1 closed_" in text


def test_uppercase_state_counted_correctly(isolated_home: Path) -> None:
    # This is the specific regression: GraphQL exporters return "OPEN".
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(1, "Open one", state="OPEN"),
            make_issue(2, "Closed one", state="CLOSED"),
        ],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "_Totals: 1 open, 1 closed_" in text


def test_open_issues_sorted_first(isolated_home: Path) -> None:
    # Even when closed issues have newer updatedAt timestamps, open
    # issues should be grouped at the top of the table.
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(1, "Old open", state="OPEN", updated_at="2025-01-01T00:00:00Z"),
            make_issue(2, "New closed", state="CLOSED", updated_at="2026-05-01T00:00:00Z"),
            make_issue(3, "Mid open", state="OPEN", updated_at="2025-06-01T00:00:00Z"),
        ],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    rows = [line for line in text.splitlines() if line.startswith("| ")]
    # Drop the header and separator rows.
    data_rows = [r for r in rows if not r.startswith("| #") and "---" not in r]
    # Open issues should appear before closed.
    assert "Mid open" in data_rows[0]  # newest open first
    assert "Old open" in data_rows[1]
    assert "New closed" in data_rows[2]


def test_pipe_in_title_escaped(isolated_home: Path) -> None:
    # Markdown table cells can't contain raw pipes; the builder must
    # escape them or the table renders broken.
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [make_issue(1, "Pipe | in title", state="open")],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "Pipe \\| in title" in text


def test_multi_repo_archives_both_appear(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home, "wg", "org/repo1",
        [make_issue(1, "First repo issue", state="open")],
    )
    write_github_archive(
        isolated_home, "wg", "org/repo2",
        [make_issue(1, "Second repo issue", state="open")],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "## org/repo1" in text
    assert "## org/repo2" in text
    assert "_Totals: 2 open, 0 closed_" in text
