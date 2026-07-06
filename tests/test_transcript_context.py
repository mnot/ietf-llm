"""Tests for transcript context enrichment.

The transcripts on disk are large WEBVTT-flavoured .md files with no
inherent meeting context. enrich_transcripts adds a small frontmatter
block at the top of each so chunks deep in the file carry attribution.

Post-reorg layout: transcripts live at
`meetings/<code>/transcripts/<YYYYMMDDHHmm>.md` (or under
`meetings/_orphans/transcripts/` when there's no meeting code).
"""

from __future__ import annotations

import os
from pathlib import Path

from ietf_llm.gather.sources.transcript_context import (
    _SENTINEL,
    enrich_transcripts,
    transcript_context,
)


def _make_minutes(cache_dir: Path, code: str, date: str) -> Path:
    """Create `meetings/<code>/minutes.md` with a Date: header."""
    d = cache_dir / "meetings" / code
    d.mkdir(parents=True, exist_ok=True)
    p = d / "minutes.md"
    p.write_text(f"# header\nDate: {date}\n")
    return p


def _make_transcript(
    cache_dir: Path, code: str, datetime_token: str, body: str = "WEBVTT\n",
) -> Path:
    """Create `meetings/<code>/transcripts/<dt>.md`."""
    d = cache_dir / "meetings" / code / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime_token}.md"
    p.write_text(body)
    return p


# --- transcript_context: filename + path parsing --------------------------


def test_orphan_transcript_matches_minutes_by_date(tmp_path: Path) -> None:
    # Transcript lives under `_orphans/` because there's no meeting
    # code in its source filename — but a minutes file with the same
    # date exists, so context resolution lifts it to that meeting.
    _make_minutes(tmp_path, "interim2026aipref01", "2026-04-15 13:15")
    _make_transcript(tmp_path, "_orphans", "202604151315")
    relpath = "meetings/_orphans/transcripts/202604151315.md"
    ctx = transcript_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.date == "2026-04-15"
    assert ctx.time == "13:15"
    assert ctx.meeting == "interim2026aipref01"
    assert ctx.label == "Interim 2026 #01"
    assert ctx.minutes_file == "meetings/interim2026aipref01/minutes.md"


def test_meeting_code_in_path_drives_context(tmp_path: Path) -> None:
    # When the transcript is filed under `meetings/<code>/`, the
    # code wins directly (no date lookup needed).
    _make_minutes(tmp_path, "ietf125", "2026-03-16 03:30")
    _make_transcript(tmp_path, "ietf125", "202603160330")
    relpath = "meetings/ietf125/transcripts/202603160330.md"
    ctx = transcript_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.meeting == "ietf125"
    assert ctx.label == "IETF 125 meeting"
    assert ctx.minutes_file == "meetings/ietf125/minutes.md"


def test_orphan_transcript_without_minutes_still_returns_context(
    tmp_path: Path,
) -> None:
    # No minutes file matches the date; we still get date/time.
    _make_transcript(tmp_path, "_orphans", "202601011000")
    relpath = "meetings/_orphans/transcripts/202601011000.md"
    ctx = transcript_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.date == "2026-01-01"
    assert ctx.meeting is None
    assert ctx.minutes_file is None


def test_non_transcript_relpath_returns_none(tmp_path: Path) -> None:
    # Path not under meetings/<code>/transcripts/ → not a transcript.
    assert transcript_context("foo.md", str(tmp_path)) is None
    assert transcript_context("meetings/ietf124/minutes.md", str(tmp_path)) is None


# --- enrich_transcripts ---------------------------------------------------


def test_enrich_prepends_header_to_real_transcript(tmp_path: Path) -> None:
    _make_minutes(tmp_path, "ietf124", "2025-11-05 21:00")
    transcript = _make_transcript(
        tmp_path, "ietf124", "202511052100",
        body="WEBVTT\n\n1 \"\" (0)\n00:00:09.659 --> ...\n",
    )
    enriched = enrich_transcripts(str(tmp_path), wg="aipref")
    assert len(enriched) == 1
    text = transcript.read_text()
    # Sentinel + meeting context + original body all present.
    assert text.startswith(_SENTINEL + "\n")
    assert "**Meeting:** IETF 124 meeting" in text
    assert "**Date / time:** 2025-11-05 21:00" in text
    assert "**Minutes:** `meetings/ietf124/minutes.md`" in text
    assert "WEBVTT" in text


def test_enrich_idempotent(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, "_orphans", "202601011000")
    enrich_transcripts(str(tmp_path), wg="aipref")
    first = transcript.read_text()
    enrich_transcripts(str(tmp_path), wg="aipref")
    second = transcript.read_text()
    # Second call must NOT stack another header on top.
    assert first == second
    assert first.count(_SENTINEL) == 1


def test_enrich_skips_files_without_recognised_pattern(tmp_path: Path) -> None:
    # A .md in the transcripts dir with a non-datetime basename gets
    # skipped — we can't infer a session datetime from "weirdthing".
    d = tmp_path / "meetings" / "_orphans" / "transcripts"
    d.mkdir(parents=True)
    (d / "weirdthing.md").write_text("WEBVTT\n")
    enriched = enrich_transcripts(str(tmp_path), wg="aipref")
    assert enriched == []


def test_enrich_handles_no_cache_dir(tmp_path: Path) -> None:
    assert enrich_transcripts(str(tmp_path / "nope")) == []


def test_enrich_orphan_without_companion_minutes(tmp_path: Path) -> None:
    transcript = _make_transcript(
        tmp_path, "_orphans", "202601011000", body="WEBVTT\nbody\n",
    )
    enriched = enrich_transcripts(str(tmp_path), wg="aipref")
    assert len(enriched) == 1
    text = transcript.read_text()
    # We have wg + date but no meeting label or minutes link.
    assert "**Working Group:** aipref" in text
    assert "**Date / time:** 2026-01-01 10:00" in text
    assert "**Meeting:**" not in text
    assert "**Minutes:**" not in text
