"""Plenary attendance: the `ietf` corpus's record and the `person_attendance` tool.

The durable guard is a writer→reader round-trip: drive the gather writer with a
stubbed Datatracker, then ask the tool about the bytes it wrote. Plus the
incremental/outage contract of the writer, name matching, and the live fallback
for a corpus that is not gathered.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from ietf_llm import live_lookup
from ietf_llm.gather.sources.meeting_registration import process_meeting_registration
from ietf_llm.gather.sources.plenary_attendance import process_plenary_attendance
from ietf_llm.log import Verbosity
from ietf_llm.mcp.attendance import tool_person_attendance
from ietf_llm.paths import (
    get_wg_file_cache_dir,
    plenary_attendance_path,
    registration_path,
)
from ietf_llm.plenary_attendance import (
    PlenaryAttendance,
    PlenarySession,
    dump,
    find_people,
    parse,
    sessions_attended,
)

# session id -> (meeting id, name); meeting id -> (number, date)
_SESSIONS = {
    "100": ("10", "Technical Plenary"),  # IETF 107: before the record starts
    "101": ("11", "IETF Plenary"),  # IETF 108
}
_MEETINGS = {"10": ("107", "2020-03-23"), "11": ("108", "2020-07-27")}
_ATTENDED: Dict[str, List[str]] = {"101": ["1", "2", "3"]}
_NAMES = {"1": "Mark Nottingham", "2": "Jane Doe", "3": "Jane Doe-Smith"}


def _reg(pid: str, first: str, last: str, kind: str, checked_in: bool) -> Dict[str, Any]:
    return {
        "person": f"/api/v1/person/person/{pid}/",
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}@example.com",
        "affiliation": "Example Corp",
        "country_code": "NZ",
        "checkedin": checked_in,
        "attended": False,
        "tickets": [
            {
                "attendance_type": f"/api/v1/name/attendancetypename/{kind}/",
                "ticket_type": "/api/v1/name/registrationtickettypename/student/",
            }
        ],
    }


_REGS: Dict[str, List[Dict[str, Any]]] = {
    "107": [],
    "108": [
        _reg("1", "Mark", "Nottingham", "remote", False),
        _reg("5", "Rita", "Registered", "onsite", True),
    ],
}


class _Datatracker:
    """Canned Datatracker; records the `attended` session ids it was asked for."""

    def __init__(self) -> None:
        self.attended_requests: List[Set[str]] = []
        self.registration_requests: List[str] = []
        self.down = False
        self.meeting_lookup_down = False
        self.listed_sessions: Optional[Set[str]] = None
        self.attended_page2_fails = False

    def __call__(self, url: str, *_args: Any, **_kw: Any) -> Optional[Dict[str, Any]]:
        if self.down:
            return None
        if "/meeting/session/" in url:
            return {
                "objects": [
                    {
                        "resource_uri": f"/api/v1/meeting/session/{sid}/",
                        "meeting": f"/api/v1/meeting/meeting/{mid}/",
                        "name": name,
                    }
                    for sid, (mid, name) in _SESSIONS.items()
                    if self.listed_sessions is None or sid in self.listed_sessions
                ],
                "meta": {"next": None},
            }
        if "/meeting/registration/" in url:
            number = url.rsplit("meeting__number=", 1)[1].split("&")[0]
            self.registration_requests.append(number)
            return {"objects": _REGS.get(number, []), "meta": {"next": None}}
        if "/meeting/meeting/" in url:
            if self.meeting_lookup_down and "id__in" in url:
                return None
            return {
                "objects": [
                    {
                        "number": number,
                        "type": "/api/v1/name/meetingtypename/ietf/",
                        "resource_uri": f"/api/v1/meeting/meeting/{mid}/",
                        "date": when,
                    }
                    for mid, (number, when) in _MEETINGS.items()
                ]
            }
        if "/meeting/attended/" in url:
            asked = set(re.search(r"session__in=([\d,]+)", url).group(1).split(","))  # type: ignore[union-attr]
            if self.attended_page2_fails and "offset=500" in url:
                return None
            self.attended_requests.append(asked)
            more = self.attended_page2_fails
            return {
                "objects": [
                    {
                        "session": f"/api/v1/meeting/session/{sid}/",
                        "person": f"/api/v1/person/person/{pid}/",
                    }
                    for sid in sorted(asked)
                    for pid in _ATTENDED.get(sid, [])
                ],
                "meta": {
                    "next": (
                        f"/api/v1/meeting/attended/?session__in={','.join(sorted(asked))}"
                        "&limit=500&offset=500"
                        if more
                        else None
                    )
                },
            }
        if "/person/person/" in url:
            ids = re.search(r"id__in=([\d,]+)", url).group(1).split(",")  # type: ignore[union-attr]
            return {
                "objects": [
                    {"resource_uri": f"/api/v1/person/person/{i}/", "name": _NAMES[i]}
                    for i in ids
                    if i in _NAMES
                ]
            }
        return None


@pytest.fixture
def dt(monkeypatch: pytest.MonkeyPatch) -> _Datatracker:
    fake = _Datatracker()
    monkeypatch.setattr("ietf_llm.gather.sources.plenary_attendance._get_json", fake)
    monkeypatch.setattr("ietf_llm.gather.sources.meetings._get_json", fake)
    monkeypatch.setattr("ietf_llm.gather.sources.meeting_registration._get_json", fake)
    monkeypatch.setattr("ietf_llm.gather.sources.meeting_registration._get_page", fake)
    return fake


_RECENT = date(2020, 8, 1)  # within a month of IETF 108
_LATER = date(2021, 1, 1)  # IETF 108 is final by now


def _write(today: date = _RECENT, wg: str = "ietf") -> str:
    cache = get_wg_file_cache_dir(wg)
    process_plenary_attendance(wg, cache, Verbosity.QUIET, today=today)
    return cache


def _stored(cache: str) -> Dict[str, Any]:
    with open(plenary_attendance_path(cache), encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


# --- writer -----------------------------------------------------------------


def test_writer_records_sessions_attendees_and_names(
    isolated_home: Path, dt: _Datatracker
) -> None:
    body = _stored(_write())
    by_id = {s["id"]: s for s in body["sessions"]}
    assert by_id["101"]["meeting"] == "108"
    assert by_id["101"]["attendees"] == ["1", "2", "3"]
    assert by_id["100"]["attendees"] == []  # listed, but no data before IETF 108
    assert body["people"]["1"] == "Mark Nottingham"


def test_writer_is_a_noop_for_other_corpora(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = _write(wg="httpbis")
    assert not os.path.exists(plenary_attendance_path(cache))
    assert dt.attended_requests == []


def test_final_sessions_are_not_refetched(
    isolated_home: Path, dt: _Datatracker
) -> None:
    _write(_LATER)
    dt.attended_requests.clear()
    _write(_LATER)
    # IETF 108 is final (old, has attendees); only the empty session is asked for.
    assert dt.attended_requests == [{"100"}]


def test_attendees_only_grow_and_an_outage_keeps_the_file(
    isolated_home: Path, dt: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _write()
    before = open(plenary_attendance_path(cache), encoding="utf-8").read()
    # A recent session whose refetch comes back empty must not lose attendees.
    monkeypatch.setitem(_ATTENDED, "101", [])
    _write()
    assert open(plenary_attendance_path(cache), encoding="utf-8").read() == before
    # And a whole outage leaves the stored file as it was.
    dt.down = True
    _write()
    assert open(plenary_attendance_path(cache), encoding="utf-8").read() == before


# --- record + matching ------------------------------------------------------


def _record() -> PlenaryAttendance:
    return PlenaryAttendance(
        sessions=[
            PlenarySession("101", "108", "2020-07-27", "IETF Plenary", ["1", "4"]),
            PlenarySession("102", "109", "2020-11-16", "IETF Plenary", ["1"]),
        ],
        people={"1": "Zoë Müller", "4": "Doe, Jane"},
    )


def test_dump_parse_round_trip_is_stable() -> None:
    text = dump(_record())
    again = parse(text)
    assert again is not None and dump(again) == text
    assert parse("{not json") is None
    assert parse(json.dumps({"version": 99})) is None


def test_find_people_folds_case_accents_and_order() -> None:
    data = _record()
    assert find_people(data.people, "zoe muller") == ["1"]
    assert find_people(data.people, "Jane Doe") == ["4"]  # `Last, First` stored form
    assert find_people(data.people, "4") == ["4"]
    assert find_people(data.people, "999") == []
    assert [s.id for s in sessions_attended(data, "1")] == ["101", "102"]


# --- the tool, over real writer output --------------------------------------


def test_tool_lists_a_persons_plenaries(isolated_home: Path, dt: _Datatracker) -> None:
    _write()
    out = tool_person_attendance("Mark Nottingham")
    assert "person 1" in out
    assert "IETF 108 · 2020-07-27 · IETF Plenary" in out
    assert "IETF 107" not in out  # no attendee rows, so not listed as attended
    assert "from IETF 108" in out  # the coverage caveat


def test_tool_exact_name_beats_substring_but_ambiguity_lists_candidates(
    isolated_home: Path, dt: _Datatracker
) -> None:
    _write()
    exact = tool_person_attendance("Jane Doe")
    assert "Jane Doe-Smith" not in exact and "person 2" in exact
    loose = tool_person_attendance("jane")
    assert "2 people match" in loose
    assert "person 2" in loose and "person 3" in loose
    assert "IETF 108" not in loose  # never guesses between them


def test_tool_unknown_person_does_not_claim_non_attendance(
    isolated_home: Path, dt: _Datatracker
) -> None:
    _write()
    out = tool_person_attendance("Nobody Atall")
    assert "No one matching" in out
    assert "absence says nothing" in out


# --- no `ietf` corpus: the replica message and the live fallback ------------


def test_tool_without_corpus_and_without_gate_points_at_gathering(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "0")
    out = tool_person_attendance("Mark Nottingham")
    assert "`ietf` corpus" in out and "ietf-llm ietf" in out


def test_tool_live_fallback_answers_without_a_gather(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    now = datetime.datetime.now(datetime.timezone.utc)

    def fake(url: str) -> Any:
        if "/person/person/?name__icontains" in url:
            return {"objects": [{"id": 1, "name": "Mark Nottingham"}]}, now, True
        if "/meeting/attended/" in url:
            return (
                {
                    "objects": [{"session": "/api/v1/meeting/session/101/"}],
                    "meta": {"next": None},
                },
                now,
                True,
            )
        if "/meeting/session/" in url:
            return (
                {
                    "objects": [
                        {
                            "id": 101,
                            "name": "IETF Plenary",
                            "meeting": "/api/v1/meeting/meeting/11/",
                        }
                    ]
                },
                now,
                True,
            )
        if "/meeting/registration/" in url:
            return (
                {
                    "objects": [
                        {
                            "meeting": "/api/v1/meeting/meeting/11/",
                            "checkedin": True,
                            "tickets": [
                                {"attendance_type": "/api/v1/name/attendancetypename/onsite/"}
                            ],
                        }
                    ],
                    "meta": {"next": None},
                },
                now,
                True,
            )
        if "/meeting/meeting/" in url:
            return (
                {"objects": [{"id": 11, "number": "108", "date": "2020-07-27"}]},
                now,
                True,
            )
        return None, now, True

    monkeypatch.setattr("ietf_llm.live_lookup.attendance._cached_json", fake)
    out = tool_person_attendance("Nottingham")
    assert "answered live" in out
    assert "IETF 108 · 2020-07-27 · IETF Plenary" in out
    assert "IETF 108 · 2020-07-27 · onsite · checked in" in out
    assert live_lookup.age_stamp(now).split("(")[0] in out


def test_tool_live_fallback_reports_an_outage_as_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    now = datetime.datetime.now(datetime.timezone.utc)
    monkeypatch.setattr(
        "ietf_llm.live_lookup.attendance._cached_json",
        lambda url: (None, now, False),
    )
    out = tool_person_attendance("Nottingham")
    assert "could not reach" in out.lower()
    assert "No one matching" not in out


# --- registration -----------------------------------------------------------


def _write_registration(today: date = _RECENT) -> str:
    cache = get_wg_file_cache_dir("ietf")
    process_meeting_registration("ietf", cache, Verbosity.QUIET, today=today)
    return cache


def _stored_registration(cache: str) -> Dict[str, Any]:
    with open(registration_path(cache), encoding="utf-8") as handle:
        return json.load(handle)  # type: ignore[no-any-return]


def test_registration_keeps_only_what_the_lookup_needs(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = _write_registration()
    raw = open(registration_path(cache), encoding="utf-8").read()
    for dropped in ("example.com", "Example Corp", "NZ", "student"):
        assert dropped not in raw
    body = json.loads(raw)
    by_num = {m["number"]: m for m in body["meetings"]}
    assert by_num["108"]["rows"] == [["1", ["remote"], 0], ["5", ["onsite"], 1]]
    assert by_num["107"]["rows"] == []
    assert body["people"]["5"] == "Rita Registered"


def test_registration_noop_for_other_corpora(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = get_wg_file_cache_dir("httpbis")
    process_meeting_registration("httpbis", cache, Verbosity.QUIET)
    assert not os.path.exists(registration_path(cache))
    assert dt.registration_requests == []


def test_old_meetings_are_final_but_a_first_run_still_fetches_them(
    isolated_home: Path, dt: _Datatracker
) -> None:
    far = date(2023, 1, 1)
    _write_registration(far)
    assert sorted(dt.registration_requests) == ["107", "108"]  # nothing assumed final
    dt.registration_requests.clear()
    _write_registration(far)
    assert dt.registration_requests == []  # 108 has rows, 107 was seen and is ancient


def test_registration_outage_keeps_the_file(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = _write_registration()
    before = open(registration_path(cache), encoding="utf-8").read()
    dt.down = True
    _write_registration()
    assert open(registration_path(cache), encoding="utf-8").read() == before


def test_tool_shows_registration_and_finds_registration_only_people(
    isolated_home: Path, dt: _Datatracker
) -> None:
    _write()
    _write_registration()
    out = tool_person_attendance("Mark Nottingham")
    assert "## Plenaries" in out and "## Registration" in out
    assert "IETF 108 · 2020-07-27 · remote" in out
    assert "checked in" not in out  # person 1 did not check in
    only = tool_person_attendance("Rita Registered")
    assert "IETF 108 · 2020-07-27 · onsite · checked in" in only
    assert "No plenary attendance is recorded" in only
    assert "not the same as attending" in only


def test_registration_line_treats_unknown_type_as_not_recorded() -> None:
    from ietf_llm.mcp.attendance import _registration_line
    from ietf_llm.meeting_registration import MeetingRegistration, Registrant

    mtg = MeetingRegistration("80", "2011-03-27")
    assert "registered (type not recorded)" in _registration_line(
        mtg, Registrant("1", ["unknown"], False)
    )
    assert "hackathon onsite + onsite · checked in" in _registration_line(
        mtg, Registrant("1", ["hackathon_onsite", "onsite"], True)
    )


# --- partial failures -------------------------------------------------------


def test_a_failed_meeting_lookup_does_not_wipe_the_plenary_record(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = _write()
    before = open(plenary_attendance_path(cache), encoding="utf-8").read()
    dt.meeting_lookup_down = True
    _write()
    assert open(plenary_attendance_path(cache), encoding="utf-8").read() == before


def test_a_short_session_listing_keeps_the_other_sessions(
    isolated_home: Path, dt: _Datatracker
) -> None:
    cache = _write()
    dt.listed_sessions = {"101"}
    _write()
    ids = {s["id"] for s in _stored(cache)["sessions"]}
    assert ids == {"100", "101"}


def test_a_partial_attendee_fetch_is_not_stored_as_final(
    isolated_home: Path, dt: _Datatracker
) -> None:
    dt.attended_page2_fails = True
    cache = _write(_LATER)
    stored = {s["id"]: s for s in _stored(cache)["sessions"]}
    assert stored["101"]["attendees"] == []  # not a truncated first page
    dt.attended_page2_fails = False
    cache = _write(_LATER)
    stored = {s["id"]: s for s in _stored(cache)["sessions"]}
    assert stored["101"]["attendees"] == ["1", "2", "3"]


def test_a_short_meeting_listing_keeps_the_other_registrations(
    isolated_home: Path, dt: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _write_registration()
    monkeypatch.setattr(
        "ietf_llm.gather.sources.meeting_registration._list_meetings",
        lambda: {"108": "2020-07-27"},
    )
    _write_registration()
    numbers = {m["number"] for m in _stored_registration(cache)["meetings"]}
    assert numbers == {"107", "108"}


def test_registration_pages_bypass_the_etag_store(
    isolated_home: Path, dt: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cached client is what gets persisted to the cloud bucket; the pages
    # carry registrants' email, so they must not go through it.
    def cached(url: str, *_a: Any, **_k: Any) -> Optional[Dict[str, Any]]:
        assert "/meeting/registration/" not in url
        return dt(url)

    monkeypatch.setattr(
        "ietf_llm.gather.sources.meeting_registration._get_json", cached
    )
    _write_registration()
    assert dt.registration_requests


# --- live name matching -----------------------------------------------------


def test_live_name_search_folds_order_and_accents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ietf_llm.live_lookup import attendance as live

    now = datetime.datetime.now(datetime.timezone.utc)
    urls: List[str] = []

    def fake(url: str) -> Any:
        urls.append(url)
        if "name__icontains" in url:
            objs = [
                {"id": 1, "name": "Mark Nottingham"},
                {"id": 9, "name": "Nottingham Forest"},
            ]
        elif "ascii__icontains=muller" in url:
            objs = [{"id": 4, "name": "Zoë Müller"}]
        else:
            objs = []
        return {"objects": objs}, now, True

    monkeypatch.setattr("ietf_llm.live_lookup.attendance._cached_json", fake)
    found, _fetched, error = live.find_people("Nottingham, Mark")
    assert error == "" and found == [("1", "Mark Nottingham")]
    assert any("name__icontains=nottingham" in u for u in urls)  # longest word only
    found, _fetched, _error = live.find_people("Zoe Muller")
    assert found == [("4", "Zoë Müller")]


# --- the tool around the edges ----------------------------------------------


def test_tool_refuses_during_a_first_gather_but_goes_live_when_it_can(
    isolated_home: Path, dt: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write()
    monkeypatch.setattr(
        "ietf_llm.mcp.attendance._first_gather_guard", lambda _wg: "still gathering"
    )
    monkeypatch.setattr("ietf_llm.mcp.attendance._answer_live", lambda _q: "LIVE")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "0")
    assert tool_person_attendance("Mark Nottingham") == "still gathering"
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    assert tool_person_attendance("Mark Nottingham") == "LIVE"


def test_tool_says_when_one_of_the_records_is_missing(
    isolated_home: Path, dt: _Datatracker
) -> None:
    _write()  # plenary record only: an older `ietf` version
    out = tool_person_attendance("Mark Nottingham")
    assert "IETF 108 · 2020-07-27 · IETF Plenary" in out
    assert "## Registration\nNot recorded in this version" in out
