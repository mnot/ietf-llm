"""`person_attendance`: which IETF plenaries a person was recorded at, and which
meetings they registered for.

Reads the records the `ietf` corpus carries (offline, so it works on a read-only
replica). Where that corpus is not gathered and live lookups are enabled, it
answers from the Datatracker instead, so a local user needs no gather.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..access_stamp import note_access
from ..freshness import freshness_line, gather_enabled, gather_suggestion
from ..meeting_registration import (
    MeetingRegistration,
    Registrant,
    RegistrationRecord,
    first_registered_meeting,
    registrations_for,
)
from ..meeting_registration import load as load_registration
from ..paths import plenary_attendance_path, registration_path
from ..plenary_attendance import (
    FIRST_RECORDED_MEETING,
    PLENARY_GROUP,
    PlenaryAttendance,
    PlenarySession,
    find_people,
    recorded_sessions,
    sessions_attended,
)
from ..plenary_attendance import load as load_plenary
from ..store.corpus import VersionVanished, get_corpus_store, pin_corpus_version
from .common import _files_dir, _first_gather_guard, _offload

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer  # pragma: no cover

#: Candidates listed when a name is ambiguous.
_MAX_CANDIDATES = 15

Registered = List[Tuple[MeetingRegistration, Registrant]]


def _not_in_version() -> str:
    return (
        f"Not recorded in this version of the `{PLENARY_GROUP}` corpus (it was "
        "gathered before this record was added) -- "
        f"{gather_suggestion(PLENARY_GROUP, purpose='to add it', force=True)}."
    )


def _caveat(registration_from: Optional[str]) -> str:
    reg = (
        f" Registration records start at IETF {registration_from}."
        if registration_from
        else ""
    )
    return (
        f"_The Datatracker records plenary attendance only from IETF "
        f"{FIRST_RECORDED_MEETING} (July 2020); for earlier plenaries there is no "
        f"data, so absence says nothing.{reg} These are the Datatracker's own "
        "records: attendance may miss people, and registering (even `onsite`) is "
        "not the same as attending. Presence, not a position on anything._"
    )


def _stored() -> (
    Tuple[Optional[PlenaryAttendance], Optional[RegistrationRecord], Optional[str]]
):
    """`(plenary, registration, busy)` from the current `ietf` corpus version.

    `(None, None, None)` when that corpus is not gathered here (or predates the
    records). `busy` is the refusal message while the corpus's first gather is
    still running, when the records may be half written."""
    version = get_corpus_store().resolve_current(PLENARY_GROUP)
    if version is None:
        return None, None, None
    for _attempt in range(2):
        try:
            with pin_corpus_version(PLENARY_GROUP, version):
                note_access(PLENARY_GROUP)
                busy = _first_gather_guard(PLENARY_GROUP)
                if busy is not None:
                    return None, None, busy
                files = _files_dir(PLENARY_GROUP)
                return (
                    load_plenary(plenary_attendance_path(files)),
                    load_registration(registration_path(files)),
                    None,
                )
        except VersionVanished as vanished:
            version = vanished.new_version
    return None, None, None


def _session_line(sess: PlenarySession) -> str:
    return f"- IETF {sess.meeting} · {sess.date} · {sess.name}"


def _registration_line(mtg: MeetingRegistration, row: Registrant) -> str:
    kinds = [t.replace("_", " ") for t in row.types if t != "unknown"]
    how = " + ".join(kinds) or "registered (type not recorded)"
    flag = " · checked in" if row.checked_in else ""
    return f"- IETF {mtg.number} · {mtg.date} · {how}{flag}"


def _render_history(
    name: str,
    person_id: str,
    plenaries: Optional[List[PlenarySession]],
    registered: Optional[Registered],
    registration_from: Optional[str],
    span: str = "",
) -> str:
    """One person's history. `None` for a part means that record is not
    available, which is different from an empty list (nothing recorded)."""
    lines = [
        f"# Meeting attendance — {name or 'person ' + person_id}"
        f" (Datatracker person {person_id})",
        "",
    ]
    if plenaries is None:
        lines.extend(["## Plenaries", _not_in_version(), ""])
    else:
        lines.append("## Plenaries")
        if plenaries:
            lines.append(f"Recorded at {len(plenaries)} plenary session(s){span}:\n")
            lines.extend(_session_line(s) for s in plenaries)
        else:
            lines.append(f"No plenary attendance is recorded{span}.")
        lines.append("")
    if registered is None:
        lines.extend(["## Registration", _not_in_version(), ""])
    else:
        lines.append("## Registration")
        if registered:
            lines.append(f"Registered for {len(registered)} meeting(s):\n")
            lines.extend(_registration_line(m, r) for m, r in registered)
        else:
            lines.append("No registration is recorded for this person.")
        lines.append("")
    lines.append(_caveat(registration_from))
    return "\n".join(lines)


def _render_candidates(
    query: str,
    rows: List[Tuple[str, str, Optional[int]]],
    total: int,
    truncated: bool = False,
) -> str:
    count = f"At least {total}" if truncated else str(total)
    lines = [
        f"{count} people match {query!r}. Call `person_attendance` again with the "
        "Datatracker person id of the one you mean:\n"
    ]
    for pid, name, records in rows[:_MAX_CANDIDATES]:
        extra = "" if records is None else f" — {records} record(s)"
        lines.append(f"- {name or '(no name)'} · person {pid}{extra}")
    if total > _MAX_CANDIDATES:
        lines.append(f"- … and {total - _MAX_CANDIDATES} more; narrow the name.")
    return "\n".join(lines)


def _answer_from_records(
    plenary: Optional[PlenaryAttendance],
    registration: Optional[RegistrationRecord],
    query: str,
) -> str:
    people: Dict[str, str] = {}
    if registration is not None:
        people.update(registration.people)
    if plenary is not None:
        people.update(plenary.people)  # the Datatracker's own name wins
    first_reg = first_registered_meeting(registration) if registration else None
    reg_from = first_reg.number if first_reg else None
    ids = find_people(people, query)
    if not ids:
        return (
            f"No one matching {query!r} is recorded at a plenary or in the "
            "registration record. Check the spelling, or pass a Datatracker "
            "person id.\n\n" + _caveat(reg_from)
        )
    if len(ids) > 1:
        rows: List[Tuple[str, str, Optional[int]]] = []
        for pid in ids:
            records = len(sessions_attended(plenary, pid)) if plenary else 0
            records += len(registrations_for(registration, pid)) if registration else 0
            rows.append((pid, people.get(pid, ""), records))
        return _render_candidates(query, rows, len(ids))

    pid = ids[0]
    attended = sessions_attended(plenary, pid) if plenary else None
    registered = registrations_for(registration, pid) if registration else None
    recorded = recorded_sessions(plenary) if plenary else []
    span = ""
    meetings = sorted({int(s.meeting) for s in recorded if s.meeting.isdigit()})
    if meetings:
        span = f" in the record (IETF {meetings[0]}–{meetings[-1]})"
    body = _render_history(
        people.get(pid, ""), pid, attended, registered, reg_from, span
    )
    if plenary is not None and recorded:
        first = min(recorded, key=lambda s: s.date).date
        missing = sorted(
            (s for s in plenary.sessions if not s.attendees and s.date >= first),
            key=lambda s: s.date,
        )
        if missing:
            labels = ", ".join(f"IETF {s.meeting}" for s in missing)
            body += f"\n\n_No plenary records yet for: {labels}._"
    stamp = freshness_line(PLENARY_GROUP)
    return f"{stamp}\n\n{body}" if stamp else body


def _answer_live(query: str) -> str:
    from .. import live_lookup  # noqa: PLC0415
    from ..live_lookup import attendance as live  # noqa: PLC0415

    candidates, fetched, error = live.find_people(query)
    if error:
        return error
    if not candidates:
        return (
            f"The Datatracker has no one matching {query!r}. Check the spelling, "
            "or pass a Datatracker person id."
        )
    if len(candidates) > 1:
        unknown: List[Tuple[str, str, Optional[int]]] = [
            (pid, name, None) for pid, name in candidates
        ]
        return _render_candidates(
            query, unknown, len(candidates), truncated=len(candidates) >= live.PAGE
        )
    pid, name = candidates[0]
    sessions, fetched, error = live.fetch_person_plenaries(pid)
    if error:
        return error
    registered, fetched, error = live.fetch_person_registrations(pid)
    if error:
        return error
    note = (
        f"_The `{PLENARY_GROUP}` corpus is not gathered here, so this was answered "
        "live from the Datatracker._\n\n"
    )
    body = _render_history(name, pid, sessions, registered, None)
    return note + body + "\n\n" + live_lookup.age_stamp(fetched)


def tool_person_attendance(person: str) -> str:
    person = (person or "").strip()
    if not person:
        return (
            "Provide a person's name (e.g. `Jane Doe`) or Datatracker person id "
            "(e.g. `103881`)."
        )
    plenary, registration, busy = _stored()
    if busy is not None and not gather_enabled():
        return busy
    if plenary is not None or registration is not None:
        return _answer_from_records(plenary, registration, person)
    if gather_enabled():
        return _answer_live(person)
    return (
        f"Plenary attendance and registration are carried by the `{PLENARY_GROUP}` "
        "corpus, which is not available here (or was gathered before they were "
        f"added) — {gather_suggestion(PLENARY_GROUP, purpose='to gather it', force=True)}."
    )


def register(server: "MCPServer") -> None:
    @server.tool()
    async def person_attendance(person: str) -> str:
        """Which IETF **plenary** sessions a person was recorded as attending,
        and which IETF meetings they **registered** for (onsite, remote or
        hackathon, and whether they checked in), from the Datatracker's own
        records. Use it for "has X attended IETF plenaries" / "which IETF
        meetings did X go to".

        `person` is a name (case, accents and `Last, First` order are folded;
        every word must match) or a Datatracker person id. If several people
        match you get a candidate list with person ids — call again with the id;
        never guess between them.

        Offline when the `ietf` corpus is gathered (it carries the records, so
        this works on a read-only server). Otherwise, if live lookups are
        enabled, it answers directly from the Datatracker with no gather.

        Plenary attendance only starts at **IETF 108 (July 2020)**, and
        registration at the meeting the answer names: for earlier meetings
        there is no data, so absence says nothing. Registering is not
        attending, and neither record is a position; do not state that someone
        did not attend from them.
        """
        return await _offload(tool_person_attendance, person)
