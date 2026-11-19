"""IETF plenary attendance: the stored record and how to query it.

The `ietf` corpus carries one file, `files/attendance/plenary.json`, written by
`gather.sources.plenary_attendance` from the Datatracker `attended` API:

    {"version": 1,
     "sessions": [{"id": "28240", "meeting": "108", "date": "2020-07-27",
                   "name": "IETF Plenary", "attendees": ["108402", ...]}, ...],
     "people": {"108402": "Display Name", ...}}

Sessions are kept apart (a plenary is not merged with side events), and
attendees are Datatracker person ids, so a lookup never depends on a name
being unique. Because the file is part of the corpus it is published with it
and reaches a read-only replica through the ordinary store seam.

This module is pure: no network, no cache-dir discovery. Both the writer and
the read tool (and its live fallback) share its data model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .people.names import name_key

#: The Datatracker group whose sessions hold the plenaries.
PLENARY_GROUP = "ietf"

SCHEMA_VERSION = 1

#: The Datatracker records attendance only from IETF 108 (July 2020). Absence
#: of a record before then says nothing about whether someone attended.
FIRST_RECORDED_MEETING = "108"


@dataclass
class PlenarySession:
    """One plenary session and who the Datatracker recorded at it."""

    id: str
    meeting: str  # meeting number, e.g. "108"
    date: str  # YYYY-MM-DD
    name: str
    attendees: List[str] = field(default_factory=list)  # person ids


@dataclass
class PlenaryAttendance:
    sessions: List[PlenarySession]
    people: Dict[str, str]  # person id -> display name


def parse(text: str) -> Optional[PlenaryAttendance]:
    """Decode the stored file, or None if it is not a usable record."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        return None
    sessions: List[PlenarySession] = []
    for row in raw.get("sessions") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        sessions.append(
            PlenarySession(
                id=str(row["id"]),
                meeting=str(row.get("meeting") or ""),
                date=str(row.get("date") or ""),
                name=str(row.get("name") or ""),
                attendees=[str(a) for a in row.get("attendees") or []],
            )
        )
    people_raw = raw.get("people")
    people = (
        {str(k): str(v) for k, v in people_raw.items()}
        if isinstance(people_raw, dict)
        else {}
    )
    return PlenaryAttendance(sessions=sessions, people=people)


def load(path: str) -> Optional[PlenaryAttendance]:
    """Read and decode `path`; None if missing or unusable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle.read())
    except OSError:
        return None


def dump(data: PlenaryAttendance) -> str:
    """Serialise deterministically (sorted), so an unchanged record is
    byte-identical and `write_if_changed` does not rewrite it."""
    sessions: List[Dict[str, Any]] = []
    for sess in sorted(data.sessions, key=lambda s: (s.date, _num(s.id))):
        sessions.append(
            {
                "id": sess.id,
                "meeting": sess.meeting,
                "date": sess.date,
                "name": sess.name,
                "attendees": sorted(set(sess.attendees), key=_num),
            }
        )
    people = {pid: data.people[pid] for pid in sorted(data.people, key=_num)}
    body = {"version": SCHEMA_VERSION, "sessions": sessions, "people": people}
    return json.dumps(body, indent=1, ensure_ascii=False) + "\n"


def _num(value: str) -> int:
    return int(value) if value.isdigit() else 0


def find_people(people: Mapping[str, str], query: str) -> List[str]:
    """Person ids in `people` (id -> name) matching `query`, best match first.

    A bare number is a Datatracker person id. Otherwise an exact name match
    (case, accents and `Last, First` order folded) wins outright; failing
    that, every query word must appear in the name."""
    query = (query or "").strip()
    if not query:
        return []
    if query.isdigit():
        return [query] if query in people else []
    want = name_key(query)
    exact = [pid for pid, name in people.items() if name_key(name) == want]
    if exact:
        return sorted(exact, key=_num)
    words = want.split()
    loose = [
        pid for pid, name in people.items() if all(w in name_key(name) for w in words)
    ]
    return sorted(loose, key=lambda pid: (name_key(people[pid]), _num(pid)))


def sessions_attended(data: PlenaryAttendance, person_id: str) -> List[PlenarySession]:
    """The plenary sessions `person_id` was recorded at, oldest first."""
    return sorted(
        (s for s in data.sessions if person_id in s.attendees),
        key=lambda s: (s.date, _num(s.id)),
    )


def recorded_sessions(data: PlenaryAttendance) -> List[PlenarySession]:
    """Sessions with at least one recorded attendee: those the record can speak for."""
    return [s for s in data.sessions if s.attendees]
