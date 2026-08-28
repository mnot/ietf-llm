"""Tests for the timeline digest.

Four event sources, each tested with synthetic cache contents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ietf_llm.net.http_metrics as http_metrics
from ietf_llm.digest import timeline
from ietf_llm.people import Registry
from ietf_llm.digest.timeline import _meeting_label, build_events, write_timeline_digest
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import (
    make_issue,
    write_cache_file,
    write_eml,
    write_github_archive,
)


def _build(wg: str = "wg") -> list:
    cache = get_wg_file_cache_dir(wg)
    return build_events(wg, cache, Registry())


# --- Source: draft publications -------------------------------------------


def test_draft_publication_event_from_i_d_action_thread(
    isolated_home: Path,
) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "I-D Action: draft-ietf-wg-foo-04.txt", "internet-drafts@ietf.org",
        "Mon, 27 Apr 2026 10:00:00 +0000",
    )
    events = _build()
    assert len(events) == 1
    assert events[0].kind == "draft-published"
    assert events[0].title == "`draft-ietf-wg-foo-04` published"
    assert events[0].when.year == 2026


# --- Source: meetings ------------------------------------------------------


def test_meeting_event_from_minutes_date_line(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "meetings/ietf124/minutes.md",
        "# Meeting Materials for IETF IETF 124 (wg)\n"
        "Date: 2025-11-05 21:00\n\n## Minutes\n",
    )
    events = _build()
    meeting_events = [e for e in events if e.kind == "meeting"]
    assert len(meeting_events) == 1
    # Title is the meeting label; the artefact list goes in the link.
    assert meeting_events[0].title == "IETF 124 meeting"
    assert meeting_events[0].when.year == 2025
    # Minutes-only session: link names the minutes file as the sole artefact.
    assert meeting_events[0].link == "minutes `meetings/ietf124/minutes.md`"


def test_meeting_event_bundles_transcript_and_slides(
    isolated_home: Path,
) -> None:
    # Consumer feedback: the user's mental model is "the meeting" as
    # one event; minutes / slides / transcripts are aspects of it,
    # not separate timeline rows. Verify all three are surfaced
    # together on a single event line.
    write_cache_file(
        isolated_home, "wg", "meetings/ietf125/minutes.md",
        "# header\nDate: 2026-03-16 03:30\n\n",
    )
    # Slide PDFs: we use the .pdf.txt extracted form as the marker.
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf125/slides/foo-00.pdf.txt", "slide content",
    )
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf125/slides/bar-00.pdf.txt", "slide content",
    )
    # Transcript filed under the meeting code's transcripts/ subdir.
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf125/transcripts/202603160330.md",
        "WEBVTT\n",
    )
    events = _build()
    meetings = [e for e in events if e.kind == "meeting"]
    assert len(meetings) == 1
    link = meetings[0].link or ""
    assert "minutes `meetings/ietf125/minutes.md`" in link
    assert (
        "transcript `meetings/ietf125/transcripts/202603160330.md`" in link
    )
    # Plural slide-decks because we have two.
    assert "2 slide decks" in link


def test_orphan_transcript_appears_without_minutes(
    isolated_home: Path,
) -> None:
    # An interim session with a transcript but no matching minutes file
    # in the cache should still surface on the timeline rather than
    # being silently dropped — the consumer report was that transcripts
    # weren't being found at all.
    write_cache_file(
        isolated_home, "wg",
        "meetings/_orphans/transcripts/202604141645.md",
        "WEBVTT\n",
    )
    events = _build()
    meetings = [e for e in events if e.kind == "meeting"]
    assert len(meetings) == 1
    link = meetings[0].link or ""
    assert (
        "transcript `meetings/_orphans/transcripts/202604141645.md`" in link
    )


def test_transcript_attaches_to_minutes_by_date(isolated_home: Path) -> None:
    # When a transcript has the generic `ietf-<wg>-…` prefix (no meeting
    # number), `transcript_context` matches it to a minutes file by date.
    # The session must show both, not split into two events.
    write_cache_file(
        isolated_home, "wg", "meetings/interim2026wg01/minutes.md",
        "# header\nDate: 2026-04-15 13:15\n",
    )
    # Transcript filed as orphan (no meeting prefix in the source);
    # transcript_context matches it to the minutes file by date.
    write_cache_file(
        isolated_home, "wg",
        "meetings/_orphans/transcripts/202604151315.md",
        "WEBVTT\n",
    )
    events = _build()
    meetings = [e for e in events if e.kind == "meeting"]
    # ONE session — not one for the minutes and one for the transcript.
    assert len(meetings) == 1
    link = meetings[0].link or ""
    assert "minutes `meetings/interim2026wg01/minutes.md`" in link
    assert (
        "transcript `meetings/_orphans/transcripts/202604151315.md`" in link
    )


def test_interim_meeting_label(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "meetings/interim2025wg09/minutes.md",
        "# header\nDate: 2025-09-30 07:15\n\n",
    )
    events = _build()
    meetings = [e for e in events if e.kind == "meeting"]
    assert len(meetings) == 1
    assert "Interim 2025 #09" in meetings[0].title


def test_minutes_without_date_line_skipped(isolated_home: Path) -> None:
    # Minutes file present but no Date: header → no event emitted.
    write_cache_file(
        isolated_home, "wg", "meetings/no-date/minutes.md",
        "no date here\n",
    )
    events = _build()
    assert events == []


# --- Source: GitHub issue events ------------------------------------------


def test_issue_open_close_events(isolated_home: Path) -> None:
    write_github_archive(
        isolated_home, "wg", "org/repo",
        [
            make_issue(
                1, "Cookie partitioning", state="closed",
                updated_at="2026-04-19T00:00:00Z",
            ),
            make_issue(
                2, "Search scope", state="open",
                updated_at="2026-05-14T00:00:00Z",
            ),
        ],
    )
    events = _build()
    kinds = sorted(e.kind for e in events)
    # 2 issues × (opened + closed for closed-one + opened-only for open-one)
    assert "issue-opened" in kinds
    assert "issue-closed" in kinds
    # No closedAt in this fixture, so updatedAt stands in as the proxy.
    closed = [e for e in events if e.kind == "issue-closed"][0]
    assert closed.when.strftime("%Y-%m-%d") == "2026-04-19"


def test_issue_closed_prefers_closedat_over_updatedat(isolated_home: Path) -> None:
    # An issue closed and then edited (or relabelled) months later would
    # otherwise report the edit as its closure. The current archive shape
    # records closedAt; use it.
    issue = make_issue(
        1, "Closed then edited", state="closed", updated_at="2026-09-01T00:00:00Z"
    )
    issue["closedAt"] = "2026-04-19T00:00:00Z"
    write_github_archive(isolated_home, "wg", "org/repo", [issue])
    closed = [e for e in _build() if e.kind == "issue-closed"][0]
    assert closed.when.strftime("%Y-%m-%d") == "2026-04-19"


def test_pull_requests_are_not_timeline_events(isolated_home: Path) -> None:
    # On a busy repo PRs outnumber issues, and a timeline where every
    # merge is an entry is one nobody reads. digests/pulls.md has them.
    from conftest import make_pull

    write_github_archive(
        isolated_home, "wg", "org/repo",
        [make_issue(1, "An issue", state="open")],
        pulls=[make_pull(2, "A merged PR")],
    )
    assert not any("A merged PR" in e.title for e in _build())


# --- Source: WGLC / adoption-call heuristics ------------------------------


def test_wglc_thread_detected(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "Working Group Last Call on our documents",
        "Chair <chair@x>",
        "Thu, 04 Sep 2025 10:00:00 +0000",
    )
    events = _build()
    wglc = [e for e in events if e.kind == "wglc"]
    assert len(wglc) == 1
    assert "Last Call" in wglc[0].title


def test_call_for_adoption_thread_detected(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "Call for Adoption: draft-it-aipref-attachment-00",
        "Chair <chair@x>",
        "Mon, 02 Jun 2025 10:00:00 +0000",
    )
    events = _build()
    adopt = [e for e in events if e.kind == "adoption-call"]
    assert len(adopt) == 1


# --- Ordering and rendering ------------------------------------------------


def test_events_sorted_most_recent_first(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "I-D Action: draft-ietf-wg-foo-00.txt", "internet-drafts@ietf.org",
        "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    write_eml(
        isolated_home, "wg", "list", 2,
        "I-D Action: draft-ietf-wg-foo-01.txt", "internet-drafts@ietf.org",
        "Mon, 01 Jun 2026 10:00:00 +0000",
    )
    events = _build()
    assert events[0].when > events[1].when


def test_digest_writes_yearly_sections(isolated_home: Path) -> None:
    # 2025 and 2026 events both present → both year headings.
    write_eml(
        isolated_home, "wg", "list", 1,
        "I-D Action: draft-ietf-wg-foo-00.txt", "internet-drafts@ietf.org",
        "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    write_eml(
        isolated_home, "wg", "list", 2,
        "I-D Action: draft-ietf-wg-foo-01.txt", "internet-drafts@ietf.org",
        "Mon, 01 Jun 2026 10:00:00 +0000",
    )
    path = write_timeline_digest(
        "wg", get_wg_file_cache_dir("wg"), Registry(),
        verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    assert "## 2026" in text
    assert "## 2025" in text
    # 2026 comes first (newest year first).
    assert text.find("## 2026") < text.find("## 2025")


def test_digest_returns_none_when_no_events(isolated_home: Path) -> None:
    assert (
        write_timeline_digest(
            "wg", get_wg_file_cache_dir("wg"), Registry(),
            verbose=Verbosity.QUIET,
        )
        is None
    )


# --- Helper: _meeting_label -----------------------------------------------


def test_meeting_label_handles_known_forms() -> None:
    assert _meeting_label("ietf124-minutes.md") == "IETF 124 meeting"
    assert _meeting_label("interim2025aipref09-minutes.md") == "Interim 2025 #09"
    # Falls back to the base if it's something we don't recognise.
    assert _meeting_label("weird-meeting-minutes.md") == "weird-meeting"


def test_datatracker_workers_share_http_metrics(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeline requests made in workers count toward the gather total."""
    http_metrics.reset()

    def fetch(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        http_metrics.record("https://datatracker.ietf.org/api/v1/", 200, 1)
        return []

    monkeypatch.setattr(timeline, "fetch_group_events", fetch)
    monkeypatch.setattr(timeline, "fetch_role_history", fetch)
    monkeypatch.setattr(timeline, "fetch_doc_events", fetch)
    monkeypatch.setattr(timeline, "fetch_ballots", fetch)

    try:
        build_events("wg", str(isolated_home), Registry())
        assert http_metrics.current().host_requests["datatracker.ietf.org"] == 4
    finally:
        http_metrics.reset()
