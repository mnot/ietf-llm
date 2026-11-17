"""Live plenary attendance for one person, straight from the Datatracker.

The fallback behind `person_attendance` when the `ietf` corpus (which carries
the stored record) is not gathered here. The Datatracker indexes `attended`
rows by person and by session group, so a person's plenary history is one
paged query plus two batched lookups — no gather required. Everything goes
through the TTL-cached, write-free `cache._cached_json`.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..gather.sources.meetings import _uri_id
from ..meeting_registration import MeetingRegistration, Registrant
from ..people.names import name_key
from ..plenary_attendance import PLENARY_GROUP, PlenarySession
from ..plenary_attendance import find_people as match_people
from .cache import _API_BASE, _DT_BASE, _cached_json, _now_utc, unreachable_note

#: Ids per `id__in` request, and a ceiling on `attended` pages followed.
_CHUNK = 50
_MAX_PAGES = 20
#: Name-search results requested (a full page means there may be more).
PAGE = 100


class _Unavailable(Exception):
    """Datatracker could not be asked; `tried_at` is when we tried."""

    def __init__(self, tried_at: datetime.datetime) -> None:
        super().__init__("Datatracker unreachable")
        self.tried_at = tried_at


def _get(url: str) -> Tuple[Optional[Dict[str, Any]], datetime.datetime]:
    body, fetched, answered = _cached_json(url)
    if not answered:
        raise _Unavailable(fetched)
    return body, fetched


def find_people(query: str) -> Tuple[List[Tuple[str, str]], datetime.datetime, str]:
    """`(candidates, fetched_at, error)`: `(person id, name)` pairs matching a
    person id or a name, by the same rules as the stored record (case, accents
    and `Last, First` order folded; every word must match). The Datatracker is
    asked for the longest word only, by name and by its ASCII form, and the
    results are filtered here. `error` is non-empty on an outage."""
    query = (query or "").strip()
    rows: List[Dict[str, Any]] = []
    try:
        if query.isdigit():
            body, fetched = _get(f"{_API_BASE}/person/person/{query}/?format=json")
            rows = [body] if body else []
        else:
            words = name_key(query).split()
            if not words:
                return [], _now_utc(), ""
            word = quote(max(words, key=len))
            for field in ("name", "ascii"):
                body, fetched = _get(
                    f"{_API_BASE}/person/person/?{field}__icontains={word}"
                    f"&limit={PAGE}&format=json"
                )
                rows.extend((body or {}).get("objects") or [])
    except _Unavailable as err:
        return [], err.tried_at, unreachable_note("the person lookup", err.tried_at)
    names = {str(r["id"]): str(r.get("name") or "") for r in rows if r.get("id")}
    if query.isdigit():
        return [(pid, name) for pid, name in names.items()], fetched, ""
    matched = match_people(names, query)
    return [(pid, names[pid]) for pid in matched], fetched, ""


def _batched(urls_for: str, ids: List[str], extra: str = "") -> List[Dict[str, Any]]:
    """All objects from `<urls_for>?id__in=<chunk>` over `ids`, chunked."""
    out: List[Dict[str, Any]] = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ",".join(ids[start : start + _CHUNK])
        body, _ = _get(
            f"{_API_BASE}/{urls_for}/?id__in={chunk}&limit=100{extra}&format=json"
        )
        out.extend((body or {}).get("objects") or [])
    return out


def fetch_person_plenaries(
    person_id: str,
) -> Tuple[List[PlenarySession], datetime.datetime, str]:
    """`(sessions, fetched_at, error)`: the plenary sessions `person_id` was
    recorded at, oldest first. `error` is non-empty on an outage."""
    fetched = _now_utc()
    try:
        session_ids: List[str] = []
        path: Optional[str] = (
            f"{_API_BASE}/meeting/attended/?person={quote(person_id)}"
            f"&session__group__acronym={PLENARY_GROUP}&limit=100&format=json"
        )
        for _ in range(_MAX_PAGES):
            if not path:
                break
            body, fetched = _get(path)
            for row in (body or {}).get("objects") or []:
                if row.get("session"):
                    session_ids.append(_uri_id(str(row["session"])))
            nxt = ((body or {}).get("meta") or {}).get("next")
            path = f"{_DT_BASE}{nxt}" if nxt else None
        ids = sorted(set(session_ids), key=int)
        sess_rows = _batched("meeting/session", ids, "&type=plenary")
        meeting_ids = sorted(
            {_uri_id(str(s["meeting"])) for s in sess_rows if s.get("meeting")},
            key=int,
        )
        meetings = {str(m["id"]): m for m in _batched("meeting/meeting", meeting_ids)}
    except _Unavailable as err:
        return [], err.tried_at, unreachable_note("plenary attendance", err.tried_at)

    sessions: List[PlenarySession] = []
    for row in sess_rows:
        meeting = meetings.get(_uri_id(str(row.get("meeting") or "")))
        if meeting is None:
            continue
        sessions.append(
            PlenarySession(
                id=str(row["id"]),
                meeting=str(meeting.get("number") or ""),
                date=str(meeting.get("date") or ""),
                name=str(row.get("name") or "Plenary"),
                attendees=[person_id],
            )
        )
    sessions.sort(key=lambda s: (s.date, int(s.id)))
    return sessions, fetched, ""


def fetch_person_registrations(
    person_id: str,
) -> Tuple[List[Tuple[MeetingRegistration, Registrant]], datetime.datetime, str]:
    """`(registrations, fetched_at, error)`: the meetings `person_id` registered
    for, oldest first, with how they took part. `error` is non-empty on an
    outage."""
    fetched = _now_utc()
    rows: List[Dict[str, Any]] = []
    try:
        path: Optional[str] = (
            f"{_API_BASE}/meeting/registration/?person={quote(person_id)}"
            "&limit=100&format=json"
        )
        for _ in range(_MAX_PAGES):
            if not path:
                break
            body, fetched = _get(path)
            rows.extend((body or {}).get("objects") or [])
            nxt = ((body or {}).get("meta") or {}).get("next")
            path = f"{_DT_BASE}{nxt}" if nxt else None
        meeting_ids = sorted(
            {_uri_id(str(r["meeting"])) for r in rows if r.get("meeting")}, key=int
        )
        meetings = {str(m["id"]): m for m in _batched("meeting/meeting", meeting_ids)}
    except _Unavailable as err:
        return [], err.tried_at, unreachable_note("registration", err.tried_at)

    out: List[Tuple[MeetingRegistration, Registrant]] = []
    for row in rows:
        meeting = meetings.get(_uri_id(str(row.get("meeting") or "")))
        if meeting is None or str(meeting.get("type") or "").rstrip("/").endswith(
            "interim"
        ):
            continue
        types = sorted(
            {
                _uri_id(str(t["attendance_type"]))
                for t in row.get("tickets") or []
                if isinstance(t, dict) and t.get("attendance_type")
            }
        )
        out.append(
            (
                MeetingRegistration(
                    str(meeting.get("number") or ""), str(meeting.get("date") or "")
                ),
                Registrant(person_id, types, bool(row.get("checkedin"))),
            )
        )
    out.sort(key=lambda pair: (pair[0].date, pair[0].number))
    return out, fetched, ""
