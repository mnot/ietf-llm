"""Tests for the filtered / paginated digest reader."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest.query import (
    Section,
    filter_rows,
    is_ballot_position,
    is_idaction_publication,
    is_mechanical_timeline_event,
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


def test_threads_sort_by_activity() -> None:
    # sort="activity" ranks threads by message count (heat), not recency.
    section = Section(
        heading="",
        columns=["Subject", "Msgs", "Participants", "First", "Last", "File"],
        rows=[
            ["Quiet", "2", "2", "2026-04-01", "2026-04-10", "a.md"],
            ["Loud", "30", "9", "2026-04-01", "2026-04-05", "b.md"],
            ["Medium", "7", "4", "2026-04-01", "2026-04-08", "c.md"],
        ],
    )
    result = filter_rows(section, "threads", {"sort": "activity"})
    assert [r[0] for r in result.rows] == ["Loud", "Medium", "Quiet"]
    # Composes with limit (top-N busiest).
    top = filter_rows(section, "threads", {"sort": "activity", "limit": 1})
    assert [r[0] for r in top.rows] == ["Loud"]


def test_mechanical_event_predicates() -> None:
    assert is_idaction_publication("`draft-x` published · `threads/t.md`")
    assert is_ballot_position("`draft-x`: Alice → No Objection · ballots/x")
    assert is_mechanical_timeline_event("`draft-x` published · `threads/t.md`")
    assert is_mechanical_timeline_event("`draft-x`: Bob → DISCUSS · ballots/x")
    # A milestone / discussion event is not mechanical.
    assert not is_mechanical_timeline_event("`rfc9931` published as RFC")
    assert not is_mechanical_timeline_event('WG Last Call thread: "..."')


def test_timeline_exclude_mechanical() -> None:
    text = (
        "# wg: timeline\n\n## 2026\n\n"
        "- **2026-05-20** — WG Last Call thread: \"WGLC for draft-x\"\n"
        "- **2026-05-18** — `draft-x`: Alice → No Objection · ballots/x\n"
        "- **2026-05-15** — `draft-x-03` published · `threads/t.md`\n"
        "- **2026-05-10** — `rfc9931` published as RFC\n"
    )
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        out = query_digest(path, "timeline", exclude_mechanical=True)
        assert "WG Last Call" in out
        assert "published as RFC" in out  # milestone kept
        assert "No Objection" not in out  # ballot position dropped
        assert "published · " not in out  # I-D Action publication dropped
    finally:
        os.remove(path)


def test_threads_subject_filter() -> None:
    # WGs without GitHub labels (TLS does most of its work on the
    # list) often cluster topics via bracketed subject-line prefixes.
    # `subject=` lets the consumer pull a topic cluster without having
    # to read the whole threads digest.
    section = Section(
        heading="",
        columns=["Subject", "Msgs", "Participants", "First", "Last", "File"],
        rows=[
            ["[MLKEM] hybrid debate", "8", "5", "2026-01-01", "2026-02-01", "a.md"],
            ["[ECH] update", "3", "2", "2026-01-15", "2026-01-20", "b.md"],
            ["mlkem followups", "1", "1", "2026-03-01", "2026-03-01", "c.md"],
        ],
    )
    result = filter_rows(section, "threads", {"subject": "mlkem"})
    # Case-insensitive substring match across the Subject column.
    assert sorted(r[0] for r in result.rows) == [
        "[MLKEM] hybrid debate",
        "mlkem followups",
    ]
    # Non-matching subject filter returns empty.
    result = filter_rows(section, "threads", {"subject": "tls13"})
    assert result.rows == []


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


def test_query_digest_empty_filter_does_not_return_full_file(tmp_path: Path) -> None:
    # Regression: a digest with no leading preamble (starts at a sub-heading)
    # plus a filter that matches nothing must return empty, not fall back to
    # dumping the entire unfiltered digest.
    path = tmp_path / "wg-_issues.md"
    path.write_text(
        "## org/repo\n\n"
        "| # | State | Title |\n"
        "|---|-------|-------|\n"
        "| 1 | CLOSED | Done thing |\n"
    )
    out = query_digest(str(path), "issues", state="open")
    assert "Done thing" not in out
    assert out.strip() == ""


def _threads_digest_file(tmp_path: Path) -> Path:
    # A single-table digest whose table sits directly under the `# `
    # title with no `## ` sub-heading — the threads / people shape.
    text = (
        "# wg: mailing list threads digest\n\n"
        "_3 threads across 9 messages._\n\n"
        "| Subject | Msgs | First | Last | File |\n"
        "|---------|------|-------|------|------|\n"
        "| Recent thing | 5 | 2026-05-01 | 2026-05-20 | `threads/a.md` |\n"
        "| Middle thing | 3 | 2026-03-01 | 2026-04-10 | `threads/b.md` |\n"
        "| Old thing | 1 | 2025-06-01 | 2025-06-01 | `threads/c.md` |\n"
    )
    path = tmp_path / "wg-_threads.md"
    path.write_text(text)
    return path


def _thread_data_rows(out: str) -> list[str]:
    return [
        line
        for line in out.splitlines()
        if line.startswith("| ")
        and "Subject" not in line
        and set(line.strip()) - set("|-: ")
    ]


def test_threads_single_table_limit_applies(tmp_path: Path) -> None:
    # Regression: the table is under the `# ` title with no `## `, so the
    # preamble must NOT swallow it. limit must actually truncate, and the
    # header row must appear exactly once (no duplicated/unfiltered copy).
    path = _threads_digest_file(tmp_path)
    out = query_digest(str(path), "threads", limit=2)
    assert len(_thread_data_rows(out)) == 2
    assert out.count("| Subject |") == 1


def test_threads_single_table_since_filters_on_last_activity(tmp_path: Path) -> None:
    path = _threads_digest_file(tmp_path)
    out = query_digest(str(path), "threads", since="2026-04-01")
    # "Old thing" (last 2025-06) drops; the other two (last in 2026) stay.
    assert "Old thing" not in out
    assert "Recent thing" in out
    assert "Middle thing" in out
    assert out.count("| Subject |") == 1


def test_threads_single_table_no_filter_verbatim(tmp_path: Path) -> None:
    path = _threads_digest_file(tmp_path)
    assert query_digest(str(path), "threads") == path.read_text()


# --- timeline filter ------------------------------------------------------


def _timeline_file(tmp_path: Path) -> Path:
    text = (
        "# wg: timeline\n\n"
        "_Preamble._\n\n"
        "## 2026\n\n"
        "- **2026-05-14** — Issue #172 opened: \"RAG\"\n"
        "- **2026-04-27** — `draft-ietf-wg-vocab-06` published\n"
        "- **2026-04-19** — Issue #160 closed: \"3.2 Respecting Preferences\"\n"
        # Real writer renders a session as its label ("IETF NNN meeting"),
        # not "meeting held" — the filter marker must match this, not a
        # hand-invented variant.
        "- **2026-03-16** — IETF 125 meeting\n\n"
        "## 2025\n\n"
        "- **2025-09-22** — WG Last Call thread\n"
    )
    path = tmp_path / "wg-_timeline.md"
    path.write_text(text)
    return path


def test_timeline_filter_by_kind(tmp_path: Path) -> None:
    path = _timeline_file(tmp_path)
    out = query_digest(str(path), "timeline", event_kind="meeting")
    assert "IETF 125 meeting" in out
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
