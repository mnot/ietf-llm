"""Meeting participation → people registry (attendance + transcript speaking).

The durable guard here is a writer→reader round-trip: drive `process_attendance`
(the gather writer) with a stubbed Datatracker, then read its `attendance.json`
back through `ingest_meeting_participation` (the registry reader). Plus focused
tests for the transcript speaker parse and the link-only contract.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ietf_llm.gather.sources.meetings import MeetingCluster, process_attendance
from ietf_llm.log import Verbosity
from ietf_llm.paths import (
    attendance_data_path,
    attendance_path,
    get_wg_file_cache_dir,
    transcripts_dir,
)
from ietf_llm.people import Person, Registry, ingest_meeting_participation

WG = "httpbis"
_PERSON1 = "https://datatracker.ietf.org/api/v1/person/person/1"
_PERSON2 = "https://datatracker.ietf.org/api/v1/person/person/2"


def _fake_get_json(url: str) -> Optional[Dict[str, Any]]:
    """Canned Datatracker responses for one IETF 125 httpbis session with two
    attendees (person 1 is a known participant, person 2 is attend-only)."""
    if "/meeting/session/" in url:
        return {
            "objects": [
                {
                    "resource_uri": "/api/v1/meeting/session/35132/",
                    "meeting": "/api/v1/meeting/meeting/500/",
                }
            ],
            "meta": {"next": None},
        }
    if "/meeting/meeting/" in url:
        return {
            "objects": [
                {
                    "number": "125",
                    "type": "/api/v1/name/meetingtypename/ietf/",
                    "resource_uri": "/api/v1/meeting/meeting/500/",
                    "date": "2026-03-14",
                }
            ]
        }
    if "/meeting/attended/" in url:
        return {
            "objects": [
                {"session": "/api/v1/meeting/session/35132/", "person": _PERSON1 + "/"},
                {"session": "/api/v1/meeting/session/35132/", "person": _PERSON2 + "/"},
            ],
            "meta": {"next": None},
        }
    if "/person/person/" in url:
        return {
            "objects": [
                {"resource_uri": _PERSON1 + "/", "name": "Mark Nottingham"},
                {"resource_uri": _PERSON2 + "/", "name": "Jane Doe"},
            ]
        }
    return None


def _ietf125_cluster() -> MeetingCluster:
    when = datetime(2026, 3, 14, tzinfo=timezone.utc)
    return MeetingCluster(
        code="ietf125",
        start=when,
        end=when,
        sessions=[{"number": "IETF 125", "date": "2026-03-14"}],
    )


def _write_attendance(monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the writer against the stub; return the cache dir it wrote into."""
    monkeypatch.setattr(
        "ietf_llm.gather.sources.meetings._get_json", _fake_get_json
    )
    cache = get_wg_file_cache_dir(WG)
    os.makedirs(os.path.join(cache, "meetings", "ietf125"), exist_ok=True)
    process_attendance(WG, cache, [_ietf125_cluster()], verbose=Verbosity.QUIET)
    return cache


# --- writer -----------------------------------------------------------------


def test_process_attendance_writes_roster(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _write_attendance(monkeypatch)
    rows = json.load(open(attendance_data_path(cache, "ietf125"), encoding="utf-8"))
    # Both attendees captured, sorted by name.
    assert [r["name"] for r in rows] == ["Jane Doe", "Mark Nottingham"]
    assert rows[1]["person"] == _PERSON1
    md = open(attendance_path(cache, "ietf125"), encoding="utf-8").read()
    assert "2 recorded attendees" in md
    assert "- Mark Nottingham" in md


# --- reader (link-only) -----------------------------------------------------


def _seed_registry() -> Registry:
    reg = Registry()
    reg.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    return reg


def test_attendance_links_known_participant_by_id(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_attendance(monkeypatch)
    reg = _seed_registry()
    # Person 1's address resolves to their Datatracker uri; person 2 (Jane)
    # has no registry identity, so stays attend-only.
    monkeypatch.setattr(
        "ietf_llm.people.meetings.resolve_addresses",
        lambda addrs, verbose=Verbosity.QUIET: {"mnot@mnot.net": _PERSON1 + "/"},
    )
    ingest_meeting_participation(reg, WG, Verbosity.QUIET)
    assert len(reg.persons) == 1  # link-only: Jane is NOT added
    assert reg.persons[0].attended_sessions == {"ietf125"}


def test_attendance_no_registry_match_stays_unlinked(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_attendance(monkeypatch)
    reg = _seed_registry()
    # No address resolves → nobody links, registry untouched.
    monkeypatch.setattr(
        "ietf_llm.people.meetings.resolve_addresses",
        lambda addrs, verbose=Verbosity.QUIET: {},
    )
    ingest_meeting_participation(reg, WG, Verbosity.QUIET)
    assert reg.persons[0].attended_sessions == set()


# --- transcript speakers ----------------------------------------------------

_TRANSCRIPT = """<!-- ietf-llm:context-header -->
# meetings/ietf125/transcripts/202603140900.md

**Working Group:** httpbis
**Date / time:** 2026-03-14 09:00
**Meeting:** IETF 125 meeting
**Minutes:** `meetings/ietf125/minutes.md`


**Mark Nottingham:** Shall we get started?

**Tommy Pauly:** Yep.

**Mark Nottingham:** Great.
"""


def _write_transcript(cache: str) -> None:
    tdir = transcripts_dir(cache, "ietf125")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "202603140900.md"), "w", encoding="utf-8") as fh:
        fh.write(_TRANSCRIPT)


def test_transcript_speaker_links_exact_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = get_wg_file_cache_dir(WG)
    os.makedirs(os.path.join(cache, "meetings", "ietf125"), exist_ok=True)
    _write_transcript(cache)
    reg = _seed_registry()  # has Mark Nottingham, not Tommy Pauly
    monkeypatch.setattr(
        "ietf_llm.people.meetings.resolve_addresses",
        lambda addrs, verbose=Verbosity.QUIET: {},
    )
    ingest_meeting_participation(reg, WG, Verbosity.QUIET)
    # Mark is a known participant → linked; Tommy isn't in the registry, and
    # the context-header labels (Working Group / Meeting / …) are never
    # treated as speakers, so no spurious Persons appear.
    assert reg.persons[0].spoke_at_meetings == {"ietf125"}
    assert len(reg.persons) == 1
    assert "Working Group" not in {p.canonical_name for p in reg.persons}


def test_header_labels_are_not_speakers(isolated_home: Path) -> None:
    from ietf_llm.people.meetings import _speakers_in  # noqa: PLC0415

    cache = get_wg_file_cache_dir(WG)
    _write_transcript(cache)
    names = _speakers_in(transcripts_dir(cache, "ietf125"))
    assert names == {"Mark Nottingham", "Tommy Pauly"}


def test_rendered_header_labels_never_parse_as_speakers(isolated_home: Path) -> None:
    # Guard the coupling both ways: feed a header rendered by the real writer
    # (`_render_header`) through the speaker parser and confirm none of its
    # labels leak in as speakers. If a label is renamed in transcript_context,
    # this fails.
    from ietf_llm.people.meetings import _speakers_in  # noqa: PLC0415
    from ietf_llm.gather.sources.transcript_context import (  # noqa: PLC0415
        TranscriptContext,
        _render_header,
    )

    ctx = TranscriptContext(wg="httpbis", date="2026-03-14", time="09:00")
    ctx.meeting = "ietf125"
    ctx.minutes_file = "meetings/ietf125/minutes.md"
    header = _render_header("meetings/ietf125/transcripts/x.md", ctx)

    cache = get_wg_file_cache_dir(WG)
    tdir = transcripts_dir(cache, "ietf125")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "x.md"), "w", encoding="utf-8") as fh:
        fh.write(header + "\n**Mark Nottingham:** Hello.\n")
    assert _speakers_in(tdir) == {"Mark Nottingham"}


def test_no_meetings_dir_is_noop(isolated_home: Path) -> None:
    reg = _seed_registry()
    ingest_meeting_participation(reg, WG, Verbosity.QUIET)  # must not raise
    assert reg.persons[0].attended_sessions == set()
