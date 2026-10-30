"""Live meeting logistics: a group's sessions at a meeting, and its upcoming
meetings, read straight from the Datatracker agenda / meeting APIs.

Backs the `meeting_schedule` read tool. `fetch_meeting_sessions` renders a
group's sessions at one meeting (numbered or interim) venue-local;
`fetch_upcoming_meetings` is the discovery listing that surfaces the (not
otherwise guessable) interim ids. Both go through the write-free
`cache._cached_json`.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from ..gather.sources.meetings import _uri_id
from .cache import (
    _API_BASE,
    _DT_BASE,
    LiveUnavailable,
    _cached_json,
    _now_utc,
    _parse_utc,
    unreachable_note,
)

_MEETECHO = "https://meetings.conf.meetecho.com"
_INTERIM_RE = re.compile(r"interim-[A-Za-z0-9-]+", re.IGNORECASE)


def is_interim_number(meeting: str) -> bool:
    """True if `meeting` is an interim id (`interim-2026-aipref-05`) rather
    than a numbered IETF meeting (`126`). Datatracker keys both kinds the
    same way in `agenda.json`, but the Meetecho URL and label differ. The id
    goes into URLs, so it is only ever letters, digits and hyphens."""
    return _INTERIM_RE.fullmatch((meeting or "").strip()) is not None


def meeting_id_label(meeting: str) -> str:
    """Human label for a meeting id: `IETF 126` for a numbered meeting, the
    canonical (lowercase) interim id (`interim-2026-aipref-05`) otherwise."""
    meeting = (meeting or "").strip()
    return meeting.lower() if is_interim_number(meeting) else f"IETF {meeting}"


@dataclass
class MeetingSession:
    """One group session at a meeting, rendered for an agenda."""

    date: str  # venue-local weekday + date, e.g. "Friday 18 July 2026"
    start_local: str  # venue-local "HH:MM"
    end_local: str
    tz: str  # IANA zone, e.g. "Europe/Vienna"
    tz_abbrev: str  # local abbreviation/offset, e.g. "CEST" or "+07"
    start_utc: str  # "YYYY-MM-DDTHH:MMZ"
    room: Optional[str]
    session_id: str
    meetecho_full: str  # numbered meetings; "" for interims (no onsite/remote split)
    meetecho_onsite: str
    agenda_url: Optional[str]
    minutes_url: Optional[str]
    # Interims carry the connection details (a Meetecho URL, a Teams note,
    # "in-room", …) in the agenda's free-text `remote_instructions`; numbered
    # meetings use the constructed Meetecho URLs above instead.
    remote_instructions: Optional[str] = None


def _parse_duration(value: str) -> datetime.timedelta:
    """Parse a `H:MM:SS` (or `H:MM`) duration; unparseable → zero."""
    parts = (value or "").split(":")
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return datetime.timedelta()
    while len(nums) < 3:
        nums.append(0)
    return datetime.timedelta(hours=nums[0], minutes=nums[1], seconds=nums[2])


def _resolve_zone(tz_name: Optional[str]) -> Optional[datetime.tzinfo]:
    """Resolve an IANA zone name to a tzinfo, or None if unavailable.

    Returns None when the name is missing or the platform has no tz database
    entry for it (missing `tzdata`); callers then fall back to UTC and label
    it so a reader is never shown a wrong local time presented as right.
    """
    if not tz_name:
        return None
    # Imported here so a platform without zoneinfo never breaks import.
    from zoneinfo import (  # noqa: PLC0415
        ZoneInfo,
        ZoneInfoNotFoundError,
    )

    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _meeting_timezone(meeting: str) -> Optional[str]:
    """The venue IANA timezone for a meeting, or None.

    The `?number=` filter accepts an interim id as well as a numbered
    meeting, so this serves both (a virtual interim's zone is usually UTC).
    """
    body, _, _ = _cached_json(
        f"{_API_BASE}/meeting/meeting/?number={meeting}&format=json"
    )
    if not body:
        return None
    objects = body.get("objects") or []
    if not objects:
        return None
    tz_name = objects[0].get("time_zone")
    return tz_name if isinstance(tz_name, str) and tz_name else None


def _build_session(
    meeting: str,
    raw: Dict[str, Any],
    tz_name: Optional[str],
    zone: Optional[datetime.tzinfo],
    is_interim: bool = False,
) -> MeetingSession:
    start_utc = _parse_utc(str(raw.get("start") or ""))
    end_utc = (
        start_utc + _parse_duration(str(raw.get("duration") or ""))
        if start_utc
        else None
    )
    start_local: Optional[datetime.datetime]
    end_local: Optional[datetime.datetime]
    if start_utc and zone is not None:
        start_local = start_utc.astimezone(zone)
        end_local = end_utc.astimezone(zone) if end_utc else None
        tz_abbrev = start_local.tzname() or (tz_name or "")
        tz_iana = tz_name or ""
    else:
        # No usable zone: present UTC and label it honestly.
        start_local = start_utc
        end_local = end_utc
        tz_abbrev = "UTC" if start_utc else ""
        tz_iana = tz_name or "UTC"
    session_id = str(raw.get("session_id") or "")
    # Numbered meetings get the constructed remote/onsite Meetecho pair; an
    # interim has no onsite room and may not be on Meetecho at all, so we
    # surface its agenda's `remote_instructions` (a Meetecho URL, a Teams
    # note, …) verbatim instead of guessing a URL.
    return MeetingSession(
        date=start_local.strftime("%A %d %B %Y") if start_local else "",
        start_local=start_local.strftime("%H:%M") if start_local else "",
        end_local=end_local.strftime("%H:%M") if end_local else "",
        tz=tz_iana,
        tz_abbrev=tz_abbrev,
        start_utc=start_utc.strftime("%Y-%m-%dT%H:%MZ") if start_utc else "",
        room=(raw.get("location") or None),
        session_id=session_id,
        meetecho_full=(
            f"{_MEETECHO}/ietf{meeting}/?session={session_id}"
            if session_id and not is_interim
            else ""
        ),
        meetecho_onsite=(
            f"{_MEETECHO}/onsite{meeting}/?session={session_id}"
            if session_id and not is_interim
            else ""
        ),
        agenda_url=(raw.get("agenda") or None),
        minutes_url=(raw.get("minutes") or None),
        remote_instructions=(
            (raw.get("remote_instructions") or None) if is_interim else None
        ),
    )


def _is_live_row(raw: Any) -> bool:
    """Whether an `agenda.json` row is a session that is actually happening.

    Datatracker keeps cancelled sessions and the vacated slot of a rescheduled
    one in the agenda, flagged only by `status` (`canceled` / `resched`); the
    agenda page shows them struck through. A row without the keys is taken as
    live, so an older or minimal shape still renders.
    """
    if not isinstance(raw, dict):
        return False
    status = raw.get("status")
    objtype = raw.get("objtype")
    return status in (None, "", "sched") and objtype in (None, "", "session")


def fetch_meeting_sessions(
    corpus: str, meeting: str
) -> Tuple[List[MeetingSession], datetime.datetime, Optional[str]]:
    """All of `corpus`'s sessions at `meeting`, venue-local.

    `meeting` is a numbered IETF meeting (`126`) or an interim id
    (`interim-2026-aipref-05`); Datatracker keys both the same way in
    `agenda.json`, so the only difference is the Meetecho/label handling in
    `_build_session`.

    Returns `(sessions, fetched_at, error)`. `error` is a human message when
    the agenda could not be read or has no published rows yet; `sessions` is
    then empty. An *empty* list with no error means the group simply has no
    session at that meeting. Cancelled and rescheduled-away rows are dropped
    (`_is_live_row`); if those are all the group has there, `error` says so
    rather than implying it never had a session. An unreachable Datatracker
    gets its own "could not reach" message, never the no-agenda one.
    """
    meeting = str(meeting).strip()
    is_interim = is_interim_number(meeting)
    # Datatracker keys interim agendas by the lowercase id; normalise so a
    # mixed-case id (which passes the case-insensitive validation) still
    # resolves rather than 404-ing as "no agenda".
    if is_interim:
        meeting = meeting.lower()
    label = meeting_id_label(meeting)
    agenda, fetched, answered = _cached_json(
        f"{_DT_BASE}/meeting/{meeting}/agenda.json"
    )
    if not answered:
        return [], fetched, unreachable_note(f"the agenda for {label}", fetched)
    if agenda is None:
        return (
            [],
            fetched,
            f"Datatracker has no agenda for {label} "
            "(no published agenda yet, or the meeting id is wrong).",
        )
    rows = agenda.get(meeting)
    if not isinstance(rows, list):
        return [], fetched, f"{label} has no published agenda yet."
    tz_name = _meeting_timezone(meeting)
    zone = _resolve_zone(tz_name)
    ours = [
        raw
        for raw in rows
        if isinstance(raw, dict) and (raw.get("group") or {}).get("acronym") == corpus
    ]
    sessions = [
        _build_session(meeting, raw, tz_name, zone, is_interim)
        for raw in ours
        if _is_live_row(raw)
    ]
    if ours and not sessions:
        return (
            [],
            fetched,
            f"`{corpus}` has no active session at {label}: the {len(ours)} "
            "session(s) listed for it there are cancelled or were moved to "
            "another slot.",
        )
    return sessions, fetched, None


@dataclass
class UpcomingMeeting:
    """A meeting `corpus` is scheduled at, for the discovery listing."""

    number: str  # "127" or "interim-2026-aipref-05"
    kind: str  # "ietf" | "interim" | ""
    date: str  # "YYYY-MM-DD" (Datatracker meeting date)
    agenda_url: str  # the human agenda page, drillable via `meeting_schedule`


def _resolve_meetings(ids: List[str]) -> Dict[str, Tuple[str, str, int, str]]:
    """Batch-resolve meeting ids → `{number: (kind, date, days, time_zone)}`
    via `id__in`.

    Raises `LiveUnavailable` if a batch could not be fetched, so a failure
    never reads as "no upcoming meetings".

    The read-path twin of `gather.sources.meetings._batch_fetch_meetings`: same
    `id__in` batching, but on the write-free `_cached_json` (not gather's
    on-disk ETag store) and keyed by meeting *number* (the caller builds
    agenda URLs / drill-in calls from it) rather than by uri-id. Keep the
    two in step if Datatracker's meeting shape changes.
    """
    out: Dict[str, Tuple[str, str, int, str]] = {}
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), 100):
        chunk = ordered[start : start + 100]
        body, fetched, answered = _cached_json(
            f"{_API_BASE}/meeting/meeting/?id__in={','.join(chunk)}"
            "&limit=100&format=json"
        )
        if not answered:
            raise LiveUnavailable(fetched)
        for meeting in (body or {}).get("objects") or []:
            number = meeting.get("number")
            if number is None:
                continue
            kind = (meeting.get("type") or "").rstrip("/").split("/")[-1]
            days = meeting.get("days")
            out[str(number)] = (
                kind,
                str(meeting.get("date") or ""),
                days if isinstance(days, int) and days > 0 else 1,
                str(meeting.get("time_zone") or ""),
            )
    return out


def _still_upcoming(
    kind: str, date: str, days: int, tz_name: str, now: datetime.datetime
) -> bool:
    """Whether a meeting dated `date` (its first day) has not yet finished.

    `Meeting.date` is day one, so comparing it to today drops a meeting from
    its second day although a group's session may be days away. An interim is
    a single day however `days` reads. The date is the venue-local day, so
    "today" is taken in the meeting's own zone (UTC when it has none): an
    evening session west of UTC is still on the day it was scheduled for.
    """
    try:
        first = datetime.date.fromisoformat(date)
    except ValueError:
        return False
    zone = _resolve_zone(tz_name) or datetime.timezone.utc
    length = 1 if kind == "interim" else days
    return first + datetime.timedelta(days=length) > now.astimezone(zone).date()


def fetch_upcoming_meetings(
    corpus: str,
) -> Tuple[List[UpcomingMeeting], datetime.datetime, Optional[str]]:
    """`corpus`'s upcoming meetings (numbered + interim), soonest first.

    Discovery for `meeting_schedule`: an interim id (`interim-2026-aipref-05`)
    isn't guessable, so this lists the group's future-dated meetings — each
    drillable by passing its `number` back to `fetch_meeting_sessions`. Walks
    the most-recent page of the group's sessions (future meetings carry the
    newest session ids) and batch-resolves their meeting metadata; the read
    stays date-level (cheap), with full per-session times on the drill-in. A
    meeting stays listed until its last (venue-local) day has passed, and a
    meeting whose only session was cancelled still appears (the listing
    cannot see session status). An unreachable
    Datatracker is reported as such, never as an empty calendar.
    """
    now = _now_utc()
    body, fetched, answered = _cached_json(
        f"{_API_BASE}/meeting/session/?group__acronym={quote(corpus, safe='')}"
        "&limit=100&order_by=-id&format=json"
    )
    if not answered:
        return (
            [],
            fetched,
            unreachable_note(f"{corpus}'s sessions", fetched),
        )
    ids: List[str] = []
    for sess in (body or {}).get("objects") or []:
        uri = sess.get("meeting")
        if uri:
            ids.append(_uri_id(str(uri)))
    try:
        meetings = _resolve_meetings(ids)
    except LiveUnavailable as err:
        return [], err.tried_at, unreachable_note(f"{corpus}'s meetings", err.tried_at)
    upcoming = [
        UpcomingMeeting(
            number=number,
            kind=kind,
            date=date,
            agenda_url=f"{_DT_BASE}/meeting/{number}/agenda",
        )
        for number, (kind, date, days, tz_name) in meetings.items()
        if _still_upcoming(kind, date, days, tz_name, now)
    ]
    upcoming.sort(key=lambda m: (m.date, m.number))
    return upcoming, fetched, None
