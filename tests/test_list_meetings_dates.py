"""`list_meetings` date column: minutes header, then agenda header, then the
earliest transcript's filename."""

from __future__ import annotations

from pathlib import Path

from ietf_llm import mcp

from conftest import write_cache_file


def _row(out: str, code: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(code))


def test_date_falls_back_to_agenda_then_transcript(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "meetings/ietf125/minutes.md", "# M\nDate: 2026-03-14\n"
    )
    write_cache_file(
        isolated_home, "wg", "meetings/ietf126/agenda.md", "# A\nDate: 2026-07-20 → 2026-07-24\n"
    )
    write_cache_file(
        isolated_home, "wg", "meetings/interim1/transcripts/202605121400.md", "hi"
    )
    write_cache_file(
        isolated_home, "wg", "meetings/interim1/transcripts/202605131400.md", "hi"
    )
    write_cache_file(isolated_home, "wg", "meetings/interim2/polls/x.md", "poll")

    out = mcp.meetings.tool_list_meetings("wg")
    assert "2026-03-14" in _row(out, "ietf125")
    assert "2026-07-20" in _row(out, "ietf126")
    assert "2026-05-12" in _row(out, "interim1")  # earliest transcript
    assert "20" not in _row(out, "interim2").replace("interim2", "")


def test_minutes_date_wins_over_agenda(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "meetings/ietf125/minutes.md", "Date: 2026-03-14\n")
    write_cache_file(isolated_home, "wg", "meetings/ietf125/agenda.md", "Date: 2026-03-01\n")
    assert "2026-03-14" in _row(mcp.meetings.tool_list_meetings("wg"), "ietf125")
