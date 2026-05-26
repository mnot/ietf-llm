"""Tests for the filtered / paginated digest reader."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest.query import (
    Section,
    filter_rows,
    parse_md_tables,
    query_digest,
    render_section,
)


# --- parser --------------------------------------------------------------


def test_parse_single_table() -> None:
    text = (
        "## fooorg/bar\n\n"
        "| # | State | Title |\n"
        "|---|-------|-------|\n"
        "| 1 | open  | First |\n"
        "| 2 | closed| Second|\n"
    )
    sections = parse_md_tables(text)
    assert len(sections) == 1
    assert sections[0].heading == "## fooorg/bar"
    assert sections[0].columns == ["#", "State", "Title"]
    assert sections[0].rows == [["1", "open", "First"], ["2", "closed", "Second"]]


def test_parse_multiple_tables_under_separate_headings() -> None:
    text = (
        "## A\n\n"
        "| X | Y |\n|---|---|\n| 1 | 2 |\n\n"
        "## B\n\n"
        "| P | Q |\n|---|---|\n| 3 | 4 |\n"
    )
    sections = parse_md_tables(text)
    assert [s.heading for s in sections] == ["## A", "## B"]


def test_parse_ignores_non_tables() -> None:
    text = "Some prose.\n\nMore prose.\n"
    assert parse_md_tables(text) == []


# --- filter_rows ---------------------------------------------------------


def _issue_section() -> Section:
    return Section(
        heading="## fooorg/bar",
        columns=["#", "State", "Title", "Labels", "Comments", "Updated", "Author"],
        rows=[
            ["1", "OPEN", "Cookie partitioning", "spec, vocabulary", "3", "2026-05-01", "Mark"],
            ["2", "OPEN", "Editorial nit", "editorial", "0", "2026-04-15", "Bob"],
            ["3", "CLOSED", "Old thing", "vocabulary", "5", "2026-03-01", "Carol"],
        ],
    )


def test_filter_by_state() -> None:
    section = _issue_section()
    result = filter_rows(section, "issues", {"state": "open"})
    assert len(result.rows) == 2
    result = filter_rows(section, "issues", {"state": "closed"})
    assert len(result.rows) == 1


def test_filter_state_case_insensitive() -> None:
    section = _issue_section()
    assert len(filter_rows(section, "issues", {"state": "OPEN"}).rows) == 2
    assert len(filter_rows(section, "issues", {"state": "Open"}).rows) == 2


def test_filter_by_label_substring() -> None:
    section = _issue_section()
    assert len(filter_rows(section, "issues", {"label": "vocab"}).rows) == 2
    assert len(filter_rows(section, "issues", {"label": "editorial"}).rows) == 1


def test_filter_by_author_substring() -> None:
    section = _issue_section()
    assert len(filter_rows(section, "issues", {"author": "Mark"}).rows) == 1


def test_filter_limit_truncates() -> None:
    section = _issue_section()
    result = filter_rows(section, "issues", {"limit": 2})
    assert len(result.rows) == 2
    assert result.rows[0][0] == "1"


def test_threads_since_until() -> None:
    section = Section(
        heading="",
        columns=["Subject", "Msgs", "Participants", "First", "Last", "File"],
        rows=[
            ["Old thread", "5", "3", "2025-01-01", "2025-01-05", "x.md"],
            ["Mid thread", "2", "2", "2025-06-01", "2025-06-15", "y.md"],
            ["New thread", "3", "3", "2026-04-01", "2026-04-10", "z.md"],
        ],
    )
    result = filter_rows(section, "threads", {"since": "2026-01-01"})
    assert [r[0] for r in result.rows] == ["New thread"]
    result = filter_rows(section, "threads", {"until": "2025-12-31"})
    assert sorted(r[0] for r in result.rows) == ["Mid thread", "Old thread"]


def test_threads_min_messages() -> None:
    section = Section(
        heading="",
        columns=["Subject", "Msgs", "Participants", "First", "Last", "File"],
        rows=[
            ["Tiny", "1", "1", "2025-01-01", "2025-01-01", "a.md"],
            ["Big", "10", "5", "2025-01-01", "2025-02-01", "b.md"],
        ],
    )
    result = filter_rows(section, "threads", {"min_messages": 5})
    assert [r[0] for r in result.rows] == ["Big"]


def test_people_role_filter() -> None:
    section = Section(
        heading="",
        columns=["Name", "Roles", "Emails", "Msgs"],
        rows=[
            ["Mark Nottingham", "Chair", "mnot@x", "90"],
            ["Eric Rescorla", "", "ekr@x", "39"],
            ["Mike Bishop", "Area Director", "mbishop@x", "0"],
        ],
    )
    chairs = filter_rows(section, "people", {"role": "Chair"})
    assert [r[0] for r in chairs.rows] == ["Mark Nottingham"]
    ads = filter_rows(section, "people", {"role": "Area Director"})
    assert [r[0] for r in ads.rows] == ["Mike Bishop"]


# --- render_section ------------------------------------------------------


def test_render_section_roundtrip() -> None:
    section = _issue_section()
    rendered = render_section(section)
    reparsed = parse_md_tables(rendered)
    assert len(reparsed) == 1
    assert reparsed[0].columns == section.columns
    assert reparsed[0].rows == section.rows


# --- query_digest end-to-end --------------------------------------------


def _issues_digest_file(tmp_path: Path) -> Path:
    text = (
        "# wg: GitHub issues digest\n\n"
        "Some preamble.\n\n"
        "## fooorg/bar\n\n"
        "| # | State | Title | Labels | Comments | Updated | Author |\n"
        "|---|-------|-------|--------|----------|---------|--------|\n"
        "| 1 | OPEN | Cookie partitioning | spec | 3 | 2026-05-01 | Mark |\n"
        "| 2 | OPEN | Editorial nit | editorial | 0 | 2026-04-15 | Bob |\n"
        "| 3 | CLOSED | Old thing | vocabulary | 5 | 2026-03-01 | Carol |\n\n"
        "_Totals: 2 open, 1 closed_\n"
    )
    path = tmp_path / "wg-_issues.md"
    path.write_text(text)
    return path


def test_query_digest_no_filters_returns_verbatim(tmp_path: Path) -> None:
    path = _issues_digest_file(tmp_path)
    assert query_digest(str(path), "issues") == path.read_text()


def test_query_digest_filters_state_open(tmp_path: Path) -> None:
    path = _issues_digest_file(tmp_path)
    out = query_digest(str(path), "issues", state="open")
    assert "Cookie partitioning" in out
    assert "Editorial nit" in out
    assert "Old thing" not in out


def test_query_digest_filters_state_and_limit(tmp_path: Path) -> None:
    path = _issues_digest_file(tmp_path)
    out = query_digest(str(path), "issues", state="open", limit=1)
    rows = [line for line in out.splitlines() if line.startswith("| ") and not line.startswith("| #")]
    # 1 separator row excluded, 1 data row kept.
    data_rows = [r for r in rows if "---" not in r]
    assert len(data_rows) == 1


def test_query_digest_drops_empty_sections(tmp_path: Path) -> None:
    path = _issues_digest_file(tmp_path)
    # state=open + author=Zelda matches nothing → no fooorg/bar heading.
    out = query_digest(str(path), "issues", state="open", author="Zelda")
    assert "## fooorg/bar" not in out


def test_query_digest_handles_missing_file(tmp_path: Path) -> None:
    assert query_digest(str(tmp_path / "nope.md"), "issues") == ""


# --- timeline filter ------------------------------------------------------


def _timeline_file(tmp_path: Path) -> Path:
    text = (
        "# wg: timeline\n\n"
        "_Preamble._\n\n"
        "## 2026\n\n"
        "- **2026-05-14** — Issue #172 opened: \"RAG\"\n"
        "- **2026-04-27** — `draft-ietf-wg-vocab-06` published\n"
        "- **2026-04-19** — Issue #160 closed: \"3.2 Respecting Preferences\"\n"
        "- **2026-03-16** — IETF 125 meeting held\n\n"
        "## 2025\n\n"
        "- **2025-09-22** — WG Last Call thread\n"
    )
    path = tmp_path / "wg-_timeline.md"
    path.write_text(text)
    return path


def test_timeline_filter_by_kind(tmp_path: Path) -> None:
    path = _timeline_file(tmp_path)
    out = query_digest(str(path), "timeline", event_kind="meeting")
    assert "IETF 125 meeting held" in out
    assert "Issue #172" not in out


def test_timeline_filter_since(tmp_path: Path) -> None:
    path = _timeline_file(tmp_path)
    out = query_digest(str(path), "timeline", since="2026-04-20")
    assert "Issue #172" in out
    assert "draft-ietf-wg-vocab-06" in out
    assert "Issue #160" not in out  # 2026-04-19 < 2026-04-20
    assert "WG Last Call" not in out


def test_timeline_limit_global_across_years(tmp_path: Path) -> None:
    path = _timeline_file(tmp_path)
    out = query_digest(str(path), "timeline", limit=2)
    # Newest 2 events across all years: 2026-05-14 and 2026-04-27.
    assert "Issue #172" in out
    assert "draft-ietf-wg-vocab-06" in out
    assert "Issue #160" not in out
    assert "## 2025" not in out  # empty year dropped


def test_timeline_drops_empty_year_headings(tmp_path: Path) -> None:
    path = _timeline_file(tmp_path)
    out = query_digest(str(path), "timeline", event_kind="meeting")
    # Only one meeting (in 2026); 2025 has none, its heading shouldn't appear.
    assert "## 2025" not in out
    assert "## 2026" in out
