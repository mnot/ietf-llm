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
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import make_issue, write_github_archive


def _digest_text(wg: str) -> str:
    path = Path(get_wg_file_cache_dir(wg)) / "digests" / "issues.md"
    return path.read_text()


def test_no_archives_no_digest_written(isolated_home: Path) -> None:
    # A WG with no github JSON archives should not get an issues digest.
    cache = Path(get_wg_file_cache_dir("wg"))
    paths = generate_digests("wg", str(cache), summarize_model=None)
    assert not (cache / "digests/issues.md").exists()
    assert all("digests/issues.md" not in p for p in paths)


def test_stale_issues_digest_removed_when_archives_disappear(
    isolated_home: Path,
) -> None:
    # A corpus gathered with GitHub then re-gathered without it (repos
    # removed / --github off) empties the archives dir. The issues
    # digest must delete the old issues.md rather than serve stale rows.
    import shutil

    from ietf_llm.paths import github_dir

    write_github_archive(
        isolated_home, "wg", "org/repo",
        [make_issue(1, "Open one", state="open")],
    )
    cache = get_wg_file_cache_dir("wg")
    generate_digests("wg", cache, summarize_model=None)
    assert (Path(cache) / "digests/issues.md").exists()
    # Re-gather drops GitHub: the archives dir is gone.
    shutil.rmtree(github_dir(cache))
    generate_digests("wg", cache, summarize_model=None)
    assert not (Path(cache) / "digests/issues.md").exists()


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


# --- Dup-of column (consumer feedback) ------------------------------------


def test_label_glossary_rendered_with_descriptions_and_counts(
    isolated_home: Path,
) -> None:
    # The archive's repo-level `labels` array is the only place a label's
    # MEANING is written down — issues carry bare names. A reader meeting
    # "Blocking Last Call" for the first time shouldn't have to infer it.
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(1, "One", labels=["blocking"]),
            make_issue(2, "Two", labels=["blocking"]),
        ],
        labels=[
            {"name": "blocking", "description": "Must land before WGLC", "color": "f00"},
            {"name": "editorial", "description": None, "color": "0f0"},
            {"name": "stale", "description": "", "color": "00f"},
        ],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "**Label vocabulary** (3 defined):" in text
    assert "- `blocking` — Must land before WGLC (2 issues)" in text
    # Defined but never applied is still worth stating: it says how the
    # repo means to organise itself.
    assert "- `editorial` (unused)" in text
    assert "- `stale` (unused)" in text


def test_label_glossary_absent_when_archive_has_no_labels_key(
    isolated_home: Path,
) -> None:
    write_github_archive(isolated_home, "wg", "org/repo", [make_issue(1, "One")])
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    assert "Label vocabulary" not in _digest_text("wg")


def test_label_glossary_survives_unfiltered_read_and_drops_when_filtered(
    isolated_home: Path,
) -> None:
    # It's a bullet list, not a table, precisely so `read_digest`'s table
    # filters can't half-eat it: unfiltered reads keep it, filtered views
    # drop it cleanly rather than emitting an emptied glossary table.
    from ietf_llm.digest.query import query_digest
    from ietf_llm.paths import digest_path

    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [make_issue(1, "One", state="OPEN", labels=["blocking"])],
        labels=[{"name": "blocking", "description": "Must land", "color": "f00"}],
    )
    cache = get_wg_file_cache_dir("wg")
    generate_digests("wg", cache, summarize_model=None)
    path = digest_path(cache, "issues")
    assert "Label vocabulary" in query_digest(path, "issues")
    filtered = query_digest(path, "issues", state="open")
    assert "Label vocabulary" not in filtered
    assert "| 1 | OPEN | One |" in filtered


def test_dup_of_column_present_and_populated(isolated_home: Path) -> None:
    # The digest table grows a new "Dup-of" column. Populated when a
    # comment calls out a duplicate, empty otherwise. Surfacing this
    # at the digest level lets a consuming LLM skip duplicate issues
    # when reading a cluster.
    write_github_archive(
        isolated_home,
        "wg",
        "org/repo",
        [
            make_issue(155, "Canonical issue", state="OPEN"),
            make_issue(
                169, "Duplicate of canonical", state="CLOSED",
                body="This appears to be a duplicate of: #155",
            ),
        ],
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    # Column header is present.
    assert "Dup-of" in text
    # The duplicate row carries #155 in its dup-of cell. We look for
    # the row that mentions the duplicate title and check it contains
    # `| #155 |` somewhere on the line.
    rows = [
        line for line in text.splitlines()
        if "Duplicate of canonical" in line
    ]
    assert rows
    assert "#155" in rows[0]
