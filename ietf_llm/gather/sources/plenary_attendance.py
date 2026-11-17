"""Gather the IETF plenary attendance record into the `ietf` corpus.

Plenaries are sessions of the Datatracker group `ietf`. The `attended` API
records who was present at each session by person id; this writes them, with
the sessions kept apart, to `files/attendance/plenary.json` (see
`ietf_llm.plenary_attendance` for the format). It rides in the corpus, not a
separate mirror, so a cloud publish carries it to the read-only replica.

Unlike the per-meeting rosters in `meetings.process_attendance`, this covers
all history regardless of the gather window: the whole record is a few
thousand rows, and a question about a person's plenary history is not bounded
by the corpus's mail window.

Incremental and best-effort. A session that ended long ago and has attendees
is final and never refetched; every other session is refetched each gather.
Attendees are only ever added to, so a partial fetch (the API client swallows
failures) can't shrink a stored record, and an outage leaves the file as it is.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set

from ...atomicio import write_if_changed
from ...log import LogLevel, Verbosity, log
from ...paths import plenary_attendance_path
from ...plenary_attendance import (
    PLENARY_GROUP,
    PlenaryAttendance,
    PlenarySession,
    dump,
    load,
)
from .datatracker import _get_json
from .meetings import (
    _batch_fetch_meetings,
    _fetch_attendance,
    _resolve_person_names,
    _uri_id,
)

_SESSION_API = (
    "https://datatracker.ietf.org/api/v1/meeting/session/"
    f"?group__acronym={PLENARY_GROUP}&type=plenary&limit=100"
)

#: A session this long past its meeting date, with attendees, is final.
_FINAL_AFTER = timedelta(days=30)


def _list_plenary_sessions() -> Dict[str, Dict[str, str]]:
    """`{session_id: {"meeting_id", "name"}}` for every plenary session."""
    out: Dict[str, Dict[str, str]] = {}
    path: Optional[str] = _SESSION_API
    while path:
        body = _get_json(path)
        if not body:
            break
        for sess in body.get("objects") or []:
            sess_uri = sess.get("resource_uri")
            meeting_uri = sess.get("meeting")
            if sess_uri and meeting_uri:
                out[_uri_id(str(sess_uri))] = {
                    "meeting_id": _uri_id(str(meeting_uri)),
                    "name": str(sess.get("name") or "Plenary"),
                }
        path = (body.get("meta") or {}).get("next") or None
    return out


def _is_final(session: PlenarySession, today: date) -> bool:
    if not session.attendees:
        return False
    try:
        held = datetime.strptime(session.date, "%Y-%m-%d").date()
    except ValueError:
        return False
    return today - held > _FINAL_AFTER


def process_plenary_attendance(
    wg_name: str,
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
    today: Optional[date] = None,
) -> None:
    """Write `attendance/plenary.json` for the `ietf` corpus; no-op for any other.

    `cache_dir` is the corpus's `files/` directory."""
    if wg_name != PLENARY_GROUP:
        return
    path = plenary_attendance_path(cache_dir)
    previous = load(path) or PlenaryAttendance(sessions=[], people={})
    prior = {s.id: s for s in previous.sessions}
    today = today or date.today()

    listed = _list_plenary_sessions()
    if not listed:
        return  # outage or empty answer: leave what we have
    meta = _batch_fetch_meetings({v["meeting_id"] for v in listed.values()})

    sessions: List[PlenarySession] = []
    for sess_id, info in listed.items():
        found = meta.get(info["meeting_id"])
        if not found:
            continue
        _display, number, when = found
        old = prior.get(sess_id)
        sessions.append(
            PlenarySession(
                id=sess_id,
                meeting=number,
                date=when,
                name=info["name"],
                attendees=list(old.attendees) if old else [],
            )
        )

    stale = [s for s in sessions if not _is_final(s, today)]
    fetched = _fetch_attendance([s.id for s in stale])
    for sess in stale:
        uris = fetched.get(sess.id) or set()
        sess.attendees = sorted(
            set(sess.attendees) | {_uri_id(uri) for uri in uris}, key=int
        )

    people = dict(previous.people)
    wanted: Set[str] = {a for s in sessions for a in s.attendees}
    missing = {f"/api/v1/person/person/{pid}" for pid in wanted - set(people)}
    for uri, name in _resolve_person_names(missing).items():
        people[_uri_id(uri)] = name

    record = PlenaryAttendance(
        sessions=sessions, people={p: n for p, n in people.items() if p in wanted}
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    changed = write_if_changed(path, dump(record))
    recorded = sum(1 for s in sessions if s.attendees)
    log(
        f"Plenary attendance: {recorded}/{len(sessions)} plenary sessions have "
        f"records, {len(wanted)} distinct attendees"
        f"{'' if changed else ' (unchanged)'}.",
        verbose,
        level=LogLevel.STATUS,
    )
