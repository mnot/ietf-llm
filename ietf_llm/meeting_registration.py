"""IETF meeting registration: the stored record and how to query it.

The `ietf` corpus carries `files/attendance/registration.json`, written by
`gather.sources.meeting_registration` from the Datatracker registration API:

    {"version": 1,
     "meetings": [{"number": "125", "date": "2026-03-14",
                   "rows": [["108402", ["onsite"], 1], ...]}, ...],
     "people": {"108402": "Display Name", ...}}

A row is `[person id, attendance types, checked-in flag]`. Only those fields
are kept: the lookup needs a person, a meeting and how they took part, so
email, affiliation, country and ticket type are deliberately not stored.

Pure: no network, no cache-dir discovery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


@dataclass
class Registrant:
    person: str
    types: List[str]  # attendance types: onsite, remote, hackathon_onsite, …
    checked_in: bool


@dataclass
class MeetingRegistration:
    number: str
    date: str  # YYYY-MM-DD
    rows: List[Registrant] = field(default_factory=list)


@dataclass
class RegistrationRecord:
    meetings: List[MeetingRegistration]
    people: Dict[str, str]  # person id -> display name


def parse(text: str) -> Optional[RegistrationRecord]:
    """Decode the stored file, or None if it is not a usable record."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        return None
    meetings: List[MeetingRegistration] = []
    for row in raw.get("meetings") or []:
        if not isinstance(row, dict) or not row.get("number"):
            continue
        rows = [
            Registrant(str(r[0]), [str(t) for t in r[1]], bool(r[2]))
            for r in row.get("rows") or []
            if isinstance(r, list) and len(r) == 3
        ]
        meetings.append(
            MeetingRegistration(
                number=str(row["number"]), date=str(row.get("date") or ""), rows=rows
            )
        )
    people_raw = raw.get("people")
    people = (
        {str(k): str(v) for k, v in people_raw.items()}
        if isinstance(people_raw, dict)
        else {}
    )
    return RegistrationRecord(meetings=meetings, people=people)


def load(path: str) -> Optional[RegistrationRecord]:
    """Read and decode `path`; None if missing or unusable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle.read())
    except OSError:
        return None


def _num(value: str) -> int:
    return int(value) if value.isdigit() else 0


def dump(record: RegistrationRecord) -> str:
    """Serialise deterministically, so an unchanged record is byte-identical
    and `write_if_changed` does not rewrite it."""
    meetings: List[Dict[str, Any]] = []
    for mtg in sorted(record.meetings, key=lambda m: (m.date, _num(m.number))):
        rows = sorted(mtg.rows, key=lambda r: _num(r.person))
        meetings.append(
            {
                "number": mtg.number,
                "date": mtg.date,
                "rows": [
                    [r.person, sorted(set(r.types)), int(r.checked_in)] for r in rows
                ],
            }
        )
    people = {pid: record.people[pid] for pid in sorted(record.people, key=_num)}
    body = {"version": SCHEMA_VERSION, "meetings": meetings, "people": people}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n"


def registrations_for(
    record: RegistrationRecord, person_id: str
) -> List["tuple[MeetingRegistration, Registrant]"]:
    """Every meeting `person_id` registered for, oldest first."""
    out = []
    for mtg in sorted(record.meetings, key=lambda m: (m.date, _num(m.number))):
        for row in mtg.rows:
            if row.person == person_id:
                out.append((mtg, row))
    return out


def first_registered_meeting(
    record: RegistrationRecord,
) -> Optional[MeetingRegistration]:
    """The earliest meeting with any registration row: where the record starts."""
    dated = [m for m in record.meetings if m.rows]
    return min(dated, key=lambda m: (m.date, _num(m.number))) if dated else None
