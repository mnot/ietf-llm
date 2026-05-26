"""Tests for transcript context enrichment.

The transcripts on disk are large WEBVTT-flavoured .md files with no
inherent meeting context. enrich_transcripts adds a small frontmatter
block at the top of each so chunks deep in the file carry attribution.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.transcript_context import (
    _SENTINEL,
    enrich_transcripts,
    transcript_context,
)


# --- transcript_context: filename parsing + meeting lookup ----------------


def test_general_transcript_uses_date_to_find_meeting(tmp_path: Path) -> None:
    # A minutes file exists whose Date: matches the transcript's date.
    (tmp_path / "interim2026aipref01-minutes.md").write_text(
        "# header\nDate: 2026-04-15 13:15\n"
    )
    ctx = transcript_context(
        "ietf-aipref-20260415-1315-transcript.md", str(tmp_path),
    )
    assert ctx is not None
    assert ctx.wg == "aipref"
    assert ctx.date == "2026-04-15"
    assert ctx.time == "13:15"
    assert ctx.meeting == "interim2026aipref01"
    assert ctx.label == "Interim 2026 #01"
    assert ctx.minutes_file == "interim2026aipref01-minutes.md"


def test_ietf_prefixed_transcript_uses_meeting_number(tmp_path: Path) -> None:
    (tmp_path / "ietf125-minutes.md").write_text(
        "# header\nDate: 2026-03-16 03:30\n"
    )
    ctx = transcript_context(
        "ietf125-aipref-20260316-0330-transcript.md", str(tmp_path),
    )
    assert ctx is not None
    assert ctx.meeting == "ietf125"
    assert ctx.label == "IETF 125 meeting"
    assert ctx.minutes_file == "ietf125-minutes.md"


def test_transcript_without_matching_minutes_still_returns_context(
    tmp_path: Path,
) -> None:
    # No minutes file with a matching date; we still get wg/date/time.
    ctx = transcript_context(
        "ietf-aipref-20260101-1000-transcript.md", str(tmp_path),
    )
    assert ctx is not None
    assert ctx.wg == "aipref"
    assert ctx.date == "2026-01-01"
    assert ctx.meeting is None
    assert ctx.minutes_file is None


def test_non_transcript_filename_returns_none(tmp_path: Path) -> None:
    assert transcript_context("foo.md", str(tmp_path)) is None
    assert transcript_context("ietf124-minutes.md", str(tmp_path)) is None


# --- enrich_transcripts ---------------------------------------------------


def test_enrich_prepends_header_to_real_transcript(tmp_path: Path) -> None:
    (tmp_path / "ietf124-minutes.md").write_text(
        "# header\nDate: 2025-11-05 21:00\n"
    )
    transcript = tmp_path / "ietf124-aipref-20251105-2100-transcript.md"
    transcript.write_text("WEBVTT\n\n1 \"\" (0)\n00:00:09.659 --> ...\n")
    enriched = enrich_transcripts(str(tmp_path))
    assert len(enriched) == 1
    text = transcript.read_text()
    # Sentinel + meeting context + original body all present.
    assert text.startswith(_SENTINEL + "\n")
    assert "**Meeting:** IETF 124 meeting" in text
    assert "**Date / time:** 2025-11-05 21:00" in text
    assert "**Minutes:** `ietf124-minutes.md`" in text
    assert "WEBVTT" in text


def test_enrich_idempotent(tmp_path: Path) -> None:
    transcript = tmp_path / "ietf-aipref-20260101-1000-transcript.md"
    transcript.write_text("WEBVTT\n")
    enrich_transcripts(str(tmp_path))
    first = transcript.read_text()
    enrich_transcripts(str(tmp_path))
    second = transcript.read_text()
    # Second call must NOT stack another header on top.
    assert first == second
    assert first.count(_SENTINEL) == 1


def test_enrich_skips_files_without_recognised_pattern(tmp_path: Path) -> None:
    # A .md ending with -transcript.md but with a junk prefix should
    # not be enriched (we can't infer anything useful).
    (tmp_path / "weirdthing-transcript.md").write_text("WEBVTT\n")
    enriched = enrich_transcripts(str(tmp_path))
    assert enriched == []


def test_enrich_handles_no_cache_dir(tmp_path: Path) -> None:
    assert enrich_transcripts(str(tmp_path / "nope")) == []


def test_enrich_without_companion_minutes_still_adds_basic_header(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "ietf-aipref-20260101-1000-transcript.md"
    transcript.write_text("WEBVTT\nbody\n")
    enriched = enrich_transcripts(str(tmp_path))
    assert len(enriched) == 1
    text = transcript.read_text()
    # We have wg + date but no meeting label or minutes link.
    assert "**Working Group:** aipref" in text
    assert "**Date / time:** 2026-01-01 10:00" in text
    assert "**Meeting:**" not in text
    assert "**Minutes:**" not in text
