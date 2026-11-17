"""Gather the IETF meeting registration record into the `ietf` corpus.

The Datatracker registration API lists who registered for each numbered
meeting and how (onsite, remote, hackathon). This writes the per-person rows to
`files/attendance/registration.json` (format: `ietf_llm.meeting_registration`),
keeping only person id, attendance type and checked-in flag, plus a display name
for lookup. It rides in the `ietf` corpus so a published version, and a seed
bundle, carry it.

Incremental and best-effort. A past meeting is final once it is a month old and
has rows (or is old enough that no registration data will ever appear), and is
never refetched. Every other meeting is fetched in full and replaces its stored
rows only if every page arrived. Stored meetings are carried forward when the
meeting listing comes back short, so neither a failed page nor a failed fetch
can shrink the record.

The registration pages carry registrants' email, which this record doesn't
keep, so they are fetched without the ETag store: that store is persisted to
the cloud bucket, and the pages should not be.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from ...atomicio import write_if_changed
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get, http_error_detail
from ...meeting_registration import (
    MeetingRegistration,
    Registrant,
    RegistrationRecord,
    dump,
    load,
)
from ...paths import registration_path
from ...plenary_attendance import PLENARY_GROUP
from .datatracker import _get_json
from .meetings import _uri_id

_DT = "https://datatracker.ietf.org"
_MEETING_API = f"{_DT}/api/v1/meeting/meeting/?type=ietf&limit=100"
_REG_API = f"{_DT}/api/v1/meeting/registration/?limit=1000&meeting__number="

#: A meeting this long past with rows is final.
_FINAL_AFTER = timedelta(days=30)
#: A meeting this long past with no rows never will have any.
_GIVE_UP_AFTER = timedelta(days=730)


def _list_meetings() -> Dict[str, str]:
    """`{number: date}` for every numbered IETF meeting."""
    out: Dict[str, str] = {}
    path: Optional[str] = _MEETING_API
    while path:
        body = _get_json(path)
        if not body:
            break
        for mtg in body.get("objects") or []:
            if mtg.get("number") is not None and mtg.get("date"):
                out[str(mtg["number"])] = str(mtg["date"])
        nxt = (body.get("meta") or {}).get("next")
        path = f"{_DT}{nxt}" if nxt else None
    return out


def _get_page(url: str) -> Optional[Dict[str, Any]]:
    """GET one registration page straight from the Datatracker, bypassing the
    ETag store (see the module docstring). None on any failure."""
    try:
        if "format=" not in url:
            url += "&format=json"
        response = governed_get(url, headers=dict(DEFAULT_HEADERS), timeout=30.0)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as err:
        log(
            f"Registration fetch failed: {http_error_detail(err)}",
            Verbosity.STATUS,
            level=LogLevel.PROGRESS,
        )
        return None
    return body if isinstance(body, dict) else None


def _fetch_meeting(
    number: str,
) -> Optional["tuple[List[Registrant], Dict[str, str]]"]:
    """All of a meeting's registrants and their names, or None if any page
    failed (a partial list must not replace a stored one)."""
    rows: Dict[str, Registrant] = {}
    names: Dict[str, str] = {}
    path: Optional[str] = f"{_REG_API}{number}"
    while path:
        body = _get_page(path)
        if body is None:
            return None
        for reg in body.get("objects") or []:
            person = reg.get("person")
            if not person:
                continue
            pid = _uri_id(str(person))
            types = sorted(
                {
                    _uri_id(str(t["attendance_type"]))
                    for t in reg.get("tickets") or []
                    if isinstance(t, dict) and t.get("attendance_type")
                }
            )
            prior = rows.get(pid)
            if prior:  # one person, several registration rows: merge them
                types = sorted(set(types) | set(prior.types))
            rows[pid] = Registrant(
                pid,
                types,
                bool(reg.get("checkedin")) or (prior is not None and prior.checked_in),
            )
            name = f"{reg.get('first_name') or ''} {reg.get('last_name') or ''}".strip()
            if name:
                names[pid] = name
        nxt = (body.get("meta") or {}).get("next")
        path = f"{_DT}{nxt}" if nxt else None
    return list(rows.values()), names


def _is_final(mtg: MeetingRegistration, today: date, seen: bool) -> bool:
    """True when `mtg` will not change. A meeting with rows is final a month
    after it is held; one with none only once we have already looked and it is
    old enough that registration data will never appear."""
    try:
        held = datetime.strptime(mtg.date, "%Y-%m-%d").date()
    except ValueError:
        return False
    age = today - held
    if mtg.rows:
        return age > _FINAL_AFTER
    return seen and age > _GIVE_UP_AFTER


def process_meeting_registration(
    wg_name: str,
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
    today: Optional[date] = None,
) -> None:
    """Write `attendance/registration.json` for the `ietf` corpus; no-op for any
    other. `cache_dir` is the corpus's `files/` directory."""
    if wg_name != PLENARY_GROUP:
        return
    path = registration_path(cache_dir)
    previous = load(path) or RegistrationRecord(meetings=[], people={})
    prior = {m.number: m for m in previous.meetings}
    today = today or date.today()

    listed = _list_meetings()
    if not listed:
        return  # outage or empty answer: leave what we have

    # Start from the stored meetings: a short listing must not drop the rest.
    by_number: Dict[str, MeetingRegistration] = dict(prior)
    people = dict(previous.people)
    for number, when in listed.items():
        old = prior.get(number)
        mtg = MeetingRegistration(number, when, list(old.rows) if old else [])
        if not _is_final(mtg, today, old is not None):
            fetched = _fetch_meeting(number)
            if fetched is not None:
                mtg.rows, names = fetched
                people.update(names)
        by_number[number] = mtg
    meetings = list(by_number.values())

    wanted = {r.person for m in meetings for r in m.rows}
    record = RegistrationRecord(
        meetings=meetings, people={p: n for p, n in people.items() if p in wanted}
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    changed = write_if_changed(path, dump(record))
    with_rows = sum(1 for m in meetings if m.rows)
    log(
        f"Meeting registration: {with_rows}/{len(meetings)} meetings have records, "
        f"{len(wanted)} distinct registrants{'' if changed else ' (unchanged)'}.",
        verbose,
        level=LogLevel.STATUS,
    )
