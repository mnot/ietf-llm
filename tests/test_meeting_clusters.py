"""Tests for interim-meeting clustering and transcript date-matching.

Datatracker lists each interim session as its own row; a multi-day
interim (or several sessions in one day) is one logical meeting.
`cluster_meetings` groups contiguous-date interim rows under one
canonical code, and `process_transcripts` uses the resulting date
spans to file interim transcripts under the right meeting instead of
orphaning them.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import ietf_llm.gather.sources.meetings as meetings
from ietf_llm.gather.sources.meetings import (
    MeetingCluster,
    cluster_meetings,
    _absorb_meeting_dir,
    _collision_free_path,
    _download_slide,
)
from ietf_llm.gather.sources.transcripts import _match_interim_cluster
from ietf_llm.utils import Verbosity


def _row(number: str, date: str) -> Dict[str, Any]:
    # date is "YYYY-MM-DD"; get_meeting_links rows also carry links,
    # but clustering only reads number + date.
    return {"number": number, "date": f"{date} 13:00-15:00 UTC", "links": []}


# --- cluster_meetings -----------------------------------------------------


def test_numbered_meetings_are_singletons() -> None:
    rows = [_row("IETF 125", "2026-03-16"), _row("IETF 124", "2025-11-03")]
    clusters = cluster_meetings(rows)
    codes = {c.code for c in clusters}
    assert codes == {"ietf125", "ietf124"}
    assert all(len(c.sessions) == 1 for c in clusters)


def test_contiguous_interims_merge_into_one() -> None:
    # Three consecutive days → one cluster, keyed by the START DATE.
    rows = [
        _row("interim-2026-aipref-05", "2026-04-14"),
        _row("interim-2026-aipref-06", "2026-04-15"),
        _row("interim-2026-aipref-07", "2026-04-16"),
    ]
    clusters = cluster_meetings(rows)
    assert len(clusters) == 1
    c = clusters[0]
    assert c.code == "interim20260414"
    assert c.start.date() == datetime(2026, 4, 14).date()
    assert c.end.date() == datetime(2026, 4, 16).date()
    assert len(c.sessions) == 3


def test_single_session_interim_is_date_coded() -> None:
    # Even a one-session interim is keyed by its date, not the
    # Datatracker sequence number.
    rows = [_row("interim-2026-webbotauth-02", "2026-02-10")]
    clusters = cluster_meetings(rows)
    assert len(clusters) == 1
    assert clusters[0].code == "interim20260210"


def test_same_day_sessions_merge() -> None:
    # Two sessions on the SAME day (gap 0) are one meeting.
    rows = [
        _row("interim-2026-aipref-05", "2026-04-14"),
        _row("interim-2026-aipref-06", "2026-04-14"),
    ]
    clusters = cluster_meetings(rows)
    assert len(clusters) == 1
    assert len(clusters[0].sessions) == 2
    assert clusters[0].code == "interim20260414"


def test_gap_over_one_day_splits() -> None:
    # 14th and 16th (gap of 2) are separate meetings.
    rows = [
        _row("interim-2026-aipref-05", "2026-04-14"),
        _row("interim-2026-aipref-06", "2026-04-16"),
    ]
    clusters = cluster_meetings(rows)
    assert len(clusters) == 2
    assert {c.code for c in clusters} == {
        "interim20260414", "interim20260416",
    }


def test_distant_interims_stay_separate() -> None:
    rows = [
        _row("interim-2025-aipref-08", "2025-09-01"),
        _row("interim-2026-aipref-05", "2026-04-14"),
        _row("interim-2026-aipref-06", "2026-04-15"),
    ]
    clusters = cluster_meetings(rows)
    by_code = {c.code: c for c in clusters}
    assert "interim20250901" in by_code
    assert len(by_code["interim20250901"].sessions) == 1
    assert "interim20260414" in by_code
    assert len(by_code["interim20260414"].sessions) == 2


def test_numbered_and_interim_dont_cross_cluster() -> None:
    # A numbered IETF meeting adjacent in date to an interim must not
    # be folded in.
    rows = [
        _row("IETF 125", "2026-04-14"),
        _row("interim-2026-aipref-05", "2026-04-15"),
    ]
    clusters = cluster_meetings(rows)
    assert {c.code for c in clusters} == {"ietf125", "interim20260415"}


def test_cluster_covers_span() -> None:
    c = MeetingCluster(
        code="x", start=datetime(2026, 4, 14), end=datetime(2026, 4, 16),
        sessions=[],
    )
    assert c.covers(datetime(2026, 4, 14, 16, 45))
    assert c.covers(datetime(2026, 4, 15))
    assert c.covers(datetime(2026, 4, 16, 23, 0))
    assert not c.covers(datetime(2026, 4, 13))
    assert not c.covers(datetime(2026, 4, 17))


# --- meeting_label --------------------------------------------------------


def test_meeting_label_numbered() -> None:
    from ietf_llm.paths import meeting_label
    assert meeting_label("ietf125") == "IETF 125 meeting"


def test_meeting_label_date_coded_interim() -> None:
    from ietf_llm.paths import meeting_label
    # WG is implicit in the cache path, so the code is just the date.
    assert meeting_label("interim20260414") == "Interim 2026-04-14"


def test_meeting_label_legacy_sequence_interim() -> None:
    from ietf_llm.paths import meeting_label
    # Old per-session codes still in a pre-clustering cache.
    assert meeting_label("interim2026aipref05") == "Interim 2026 #05"


def test_meeting_label_unknown_passthrough() -> None:
    from ietf_llm.paths import meeting_label
    assert meeting_label("_orphans") == "_orphans"


# --- transcript matching --------------------------------------------------


def test_match_interim_cluster_by_date() -> None:
    clusters: List[MeetingCluster] = [
        MeetingCluster(
            code="interim20260414",
            start=datetime(2026, 4, 14), end=datetime(2026, 4, 16),
            sessions=[],
        ),
    ]
    # All three days resolve to the one cluster.
    assert _match_interim_cluster("20260414", clusters) == "interim20260414"
    assert _match_interim_cluster("20260415", clusters) == "interim20260414"
    assert _match_interim_cluster("20260416", clusters) == "interim20260414"


def test_match_returns_none_when_no_cluster_covers() -> None:
    clusters = [
        MeetingCluster(
            code="interim20260414",
            start=datetime(2026, 4, 14), end=datetime(2026, 4, 16),
            sessions=[],
        ),
    ]
    assert _match_interim_cluster("20260101", clusters) is None


def test_match_returns_none_without_clusters() -> None:
    assert _match_interim_cluster("20260414", None) is None
    assert _match_interim_cluster("20260414", []) is None


# --- absorb / collision helpers -------------------------------------------


def test_collision_free_path_increments(tmp_path: Path) -> None:
    p = tmp_path / "slide.pdf"
    assert _collision_free_path(str(p)) == str(p)  # no collision
    p.write_text("x")
    assert _collision_free_path(str(p)) == str(tmp_path / "slide-2.pdf")
    (tmp_path / "slide-2.pdf").write_text("y")
    assert _collision_free_path(str(p)) == str(tmp_path / "slide-3.pdf")


def test_absorb_meeting_dir_moves_and_removes(tmp_path: Path) -> None:
    src = tmp_path / "interim06"
    dst = tmp_path / "interim05"
    (src / "slides").mkdir(parents=True)
    (src / "slides" / "deck.pdf").write_text("deck6")
    (dst / "slides").mkdir(parents=True)
    (dst / "slides" / "deck.pdf").write_text("deck5")  # name collision
    _absorb_meeting_dir(str(src), str(dst))
    # Source gone; both decks preserved under dst (one renamed).
    assert not src.exists()
    decks = sorted(p.name for p in (dst / "slides").iterdir())
    assert decks == ["deck-2.pdf", "deck.pdf"]


def test_absorb_no_op_when_src_is_dst(tmp_path: Path) -> None:
    d = tmp_path / "interim05"
    (d / "slides").mkdir(parents=True)
    (d / "slides" / "deck.pdf").write_text("x")
    _absorb_meeting_dir(str(d), str(d))
    # Must NOT delete itself.
    assert (d / "slides" / "deck.pdf").exists()


# --- _download_slide skip-check -------------------------------------------


def test_download_slide_skips_when_pdf_exists(tmp_path: Path, monkeypatch) -> None:
    # Local mode: an existing .pdf means skip the download (unchanged).
    out = tmp_path / "slides"
    out.mkdir()
    (out / "deck.pdf").write_bytes(b"%PDF-1.4 stub")
    called = {"n": 0}

    def fake_dl(url: str, dest: str, verbose: Verbosity) -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr(meetings, "_download_if_pdf", fake_dl)
    assert _download_slide("https://x/deck.pdf", str(out), Verbosity.QUIET) is False
    assert called["n"] == 0


def test_download_slide_skips_when_txt_exists(tmp_path: Path, monkeypatch) -> None:
    # Suppressed mode dropped the .pdf but kept .pdf.txt — the .txt is the
    # idempotency token, so a re-gather must not re-download the deck.
    out = tmp_path / "slides"
    out.mkdir()
    (out / "deck.pdf.txt").write_text("extracted")
    called = {"n": 0}

    def fake_dl(url: str, dest: str, verbose: Verbosity) -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr(meetings, "_download_if_pdf", fake_dl)
    assert _download_slide("https://x/deck.pdf", str(out), Verbosity.QUIET) is False
    assert called["n"] == 0


def test_download_slide_downloads_when_neither_exists(
    tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "slides"
    called = {"n": 0}

    def fake_dl(url: str, dest: str, verbose: Verbosity) -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr(meetings, "_download_if_pdf", fake_dl)
    assert _download_slide("https://x/deck.pdf", str(out), Verbosity.QUIET) is True
    assert called["n"] == 1
