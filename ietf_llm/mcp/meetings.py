"""Meeting tools: list_meetings, read_minutes, meeting_schedule."""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, List

from ..paths import (
    agenda_path,
    attendance_data_path,
    attendance_path,
    meetings_dir,
    minutes_path,
    polls_dir,
    transcripts_dir,
)
from .common import _files_dir, _offload, _requires_corpus, _with_freshness

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


#: Line cap for a single gathered minutes read, so a pathological transcript-
#: sized minutes file cannot blow the context window in one call.
_MINUTES_MAX_LINES = 2000


def _read_text_capped(path: str, max_lines: int, relpath: str = "") -> str:
    """Read a file, truncating past `max_lines` with an actionable pointer to
    page the rest. `relpath` (the corpus-relative path) makes the pointer name
    the exact `read_file_section` call. Empty string if unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    if len(lines) > max_lines:
        more = (
            f'`read_file_section(file="{relpath}", start_line={max_lines + 1})`'
            if relpath
            else "`read_file_section`"
        )
        return "".join(lines[:max_lines]) + (
            f"\n\n_(truncated at {max_lines} lines — read the rest with " f"{more})_\n"
        )
    return "".join(lines)


def _session_date(cache: str, code: str) -> str:
    """Best-effort session date from a meeting's minutes header, or ''."""
    try:
        with open(minutes_path(cache, code), encoding="utf-8") as handle:
            head = handle.read(2000)
    except OSError:
        return ""
    match = re.search(r"(?im)^\s*Date:\s*(\d{4}-\d{2}-\d{2})", head)
    return match.group(1) if match else ""


def _dir_md_count(directory: str) -> int:
    """Number of `.md` files directly in `directory` (0 if it is absent)."""
    if not os.path.isdir(directory):
        return 0
    return len([f for f in os.listdir(directory) if f.endswith(".md")])


def _attendee_count(cache: str, code: str) -> int:
    """Number of recorded attendees for a meeting (0 if no roster)."""
    try:
        with open(attendance_data_path(cache, code), encoding="utf-8") as handle:
            rows = json.load(handle)
    except (OSError, ValueError):
        return 0
    return len(rows) if isinstance(rows, list) else 0


def _session_artifacts(cache: str, code: str) -> str:
    """Compact 'minutes · agenda · N transcripts · N polls' inventory."""
    parts: List[str] = []
    if os.path.isfile(minutes_path(cache, code)):
        parts.append("minutes")
    if os.path.isfile(agenda_path(cache, code)):
        parts.append("agenda")
    n_tx = _dir_md_count(transcripts_dir(cache, code))
    if n_tx:
        parts.append(f"{n_tx} transcript{'s' if n_tx != 1 else ''}")
    n_polls = _dir_md_count(polls_dir(cache, code))
    if n_polls:
        parts.append(f"{n_polls} poll{'s' if n_polls != 1 else ''}")
    n_att = _attendee_count(cache, code)
    if n_att:
        parts.append(f"{n_att} attendees")
    return " · ".join(parts) or "(no artifacts)"


def _sessions_listing(wg: str, cache: str) -> str:
    """Body of `tool_list_meetings` (undecorated) so `read_minutes` can reuse
    it without re-entering the corpus-version pin."""
    mdir = meetings_dir(cache)
    codes = (
        sorted(
            name
            for name in os.listdir(mdir)
            if os.path.isdir(os.path.join(mdir, name)) and not name.startswith("_")
        )
        if os.path.isdir(mdir)
        else []
    )
    if not codes:
        return f"No meetings gathered for {wg}."
    rows = [
        (code, _session_date(cache, code), _session_artifacts(cache, code))
        for code in codes
    ]
    code_w = max(len(c) for c, _, _ in rows)
    date_w = max((len(d) for _, d, _ in rows), default=0)
    lines = [f"{c.ljust(code_w)}  {d.ljust(date_w)}  {a}".rstrip() for c, d, a in rows]
    return (
        f"Gathered meetings for {wg} (code · date · artifacts). "
        f'Read one with `read_minutes(corpus="{wg}", meeting="<code>")`.\n\n'
        + "\n".join(lines)
    )


@_requires_corpus
def tool_list_meetings(wg: str) -> str:
    return _with_freshness(wg, _sessions_listing(wg, _files_dir(wg)))


def _read_polls(cache: str, code: str) -> str:
    """Concatenate a meeting's gathered poll files (small), or '' if none."""
    pdir = polls_dir(cache, code)
    if not os.path.isdir(pdir):
        return ""
    chunks = []
    for name in sorted(os.listdir(pdir)):
        if not name.endswith(".md"):
            continue
        fpath = os.path.join(pdir, name)
        text = _read_text_capped(fpath, 500, relpath=os.path.relpath(fpath, cache))
        if text.strip():
            chunks.append(text)
    return "\n\n".join(chunks)


@_requires_corpus
def tool_read_minutes(wg: str, meeting: str = "") -> str:
    cache = _files_dir(wg)
    if not meeting:
        return _with_freshness(
            wg,
            "Pass a `meeting` code (e.g. `ietf125`). Call "
            f'`list_meetings("{wg}")` to see the gathered meetings and their '
            "codes.",
        )
    path = minutes_path(cache, meeting)
    if not os.path.isfile(path):
        return _with_freshness(
            wg,
            f"No minutes gathered for meeting '{meeting}' in {wg}. Call "
            f'`list_meetings("{wg}")` to see which meetings were gathered.',
        )
    minutes_text = _read_text_capped(
        path, _MINUTES_MAX_LINES, relpath=os.path.relpath(path, cache)
    )
    body = f"# Minutes — {wg} {meeting}\n\n{minutes_text}"
    polls = _read_polls(cache, meeting)
    if polls:
        body += (
            "\n\n## Polls\n\n_Raw poll tallies — a poll is a sense of the room, "
            "not a decision; the chair declares consensus._\n\n" + polls
        )
    n_att = _attendee_count(cache, meeting)
    if n_att:
        roster = os.path.relpath(attendance_path(cache, meeting), cache)
        body += (
            f"\n\n## Attendance\n\n_{n_att} recorded attendees — presence in "
            "the room, NOT a position on any question. Read the full roster "
            f'with `read_file_section(file="{roster}")`._\n'
        )
    return _with_freshness(wg, body)


def _render_upcoming_meetings(corpus: str) -> str:
    """The discovery listing: `corpus`'s upcoming numbered + interim meetings,
    each drillable by passing its id back as `meeting`."""
    from .. import live_lookup  # pylint: disable=import-outside-toplevel

    meetings, fetched, error = live_lookup.fetch_upcoming_meetings(corpus)
    if error:
        return error
    if not meetings:
        return (
            f"No upcoming meetings are scheduled for `{corpus}` — it may have "
            "nothing on the calendar, or none published yet.\n\n"
            + live_lookup.age_stamp(fetched)
        )
    lines = [f"# {corpus} — {len(meetings)} upcoming meeting(s)\n"]
    for mtg in meetings:
        # One label source (`meeting_id_label`) for both surfaces; the raw id
        # stays visible on the Logistics line below.
        lines.append(f"- **{mtg.date}** — {live_lookup.meeting_id_label(mtg.number)}")
        lines.append(f"  - Agenda: {mtg.agenda_url}")
        lines.append(f"  - Logistics: `meeting_schedule({corpus!r}, {mtg.number!r})`")
    lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def tool_meeting_schedule(corpus: str, meeting: str = "") -> str:
    """Render a group's live session logistics at a numbered or interim meeting.

    With no `meeting`, lists the group's upcoming meetings (discovery, since
    an interim id isn't guessable). Lazily imports `live_lookup` (the one
    read-path network module) so the default offline read path never pulls it
    in; registered only behind the gather gate. Times are venue-local,
    converted from the agenda's UTC.
    """
    from .. import live_lookup  # pylint: disable=import-outside-toplevel

    # pylint: disable-next=import-outside-toplevel
    from ..gather import runner as gather_runner

    corpus = (corpus or "").strip()
    meeting = str(meeting or "").strip()
    if not corpus:
        return "Provide a Working Group shortname (e.g. `httpbis`)."
    if not gather_runner.valid_corpus_name(corpus):
        return f"'{corpus}' is not a valid corpus name."
    if not meeting:
        return _render_upcoming_meetings(corpus)
    if not (meeting.isdigit() or live_lookup.is_interim_number(meeting)):
        return (
            "Provide a numbered IETF meeting (e.g. `126`) or an interim id "
            "(e.g. `interim-2026-aipref-05`), or omit it to list this group's "
            "upcoming meetings."
        )
    return _render_meeting_sessions(corpus, meeting)


def _render_meeting_sessions(corpus: str, meeting: str) -> str:
    """Render one meeting's sessions (numbered or interim), venue-local."""
    from .. import live_lookup  # pylint: disable=import-outside-toplevel

    label = live_lookup.meeting_id_label(meeting)
    sessions, fetched, error = live_lookup.fetch_meeting_sessions(corpus, meeting)
    if error:
        return error
    if not sessions:
        return (
            f"No session for `{corpus}` is scheduled at {label} — the "
            "group may not be meeting, or the agenda may not list it yet.\n\n"
            + live_lookup.age_stamp(fetched)
        )

    lines = [f"# {corpus} at {label} — {len(sessions)} session(s)\n"]
    for idx, sess in enumerate(sessions, start=1):
        lines.append(f"## Session {idx}" if len(sessions) > 1 else "## Session")
        local = f"{sess.start_local}–{sess.end_local} {sess.tz_abbrev}".strip()
        lines.append(f"- **When:** {sess.date}, {local}  ({sess.tz})")
        if sess.start_utc:
            lines.append(f"- **Starts (UTC):** {sess.start_utc}")
        if sess.room:
            lines.append(f"- **Room:** {sess.room}")
        if sess.session_id:
            lines.append(f"- **Session id:** {sess.session_id}")
        if sess.meetecho_full:
            lines.append(f"- **Meetecho (remote):** {sess.meetecho_full}")
        if sess.meetecho_onsite:
            lines.append(f"- **Meetecho (onsite):** {sess.meetecho_onsite}")
        if sess.remote_instructions:
            lines.append(f"- **Remote:** {sess.remote_instructions}")
        if sess.agenda_url:
            lines.append(f"- **Agenda:** {sess.agenda_url}")
        if sess.minutes_url:
            lines.append(f"- **Minutes:** {sess.minutes_url}")
        lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def list_meetings(corpus: str) -> str:
        """List a corpus's gathered meetings — each meeting code with its date
        and which artifacts are present (minutes, agenda, transcripts, polls).
        Offline, from the cache. Use it to find the `meeting` code to pass to
        `read_minutes`, or to see which meetings were captured.
        """
        return await _offload(tool_list_meetings, corpus)

    @server.tool()
    async def read_minutes(corpus: str, meeting: str = "") -> str:
        """Read the gathered minutes for one meeting, plus any recorded poll
        tallies. Offline, from the cache; the authoritative record of what a
        meeting discussed and decided.

        Requires the `meeting` code (e.g. `ietf125`, `interim20260401`) — call
        `list_meetings` first to find it. The appended polls are raw
        sense-of-the-room tallies, NOT decisions — the chair declares consensus
        (see `read_ietf_interpretation_norms`). When an attendance record
        exists, a count and a pointer to the full roster are appended;
        attendance is presence, not a position.
        """
        return await _offload(tool_read_minutes, corpus, meeting)


def register_live(server: "FastMCP") -> None:
    @server.tool()
    async def meeting_schedule(corpus: str, meeting: str = "") -> str:
        """A group's **live** meeting schedule from Datatracker — its session
        logistics at an IETF meeting (building an agenda is the obvious
        case). The live counterpart to the offline gathered record
        (`list_meetings` / `read_minutes`).

        Handles both **numbered** meetings (e.g. `126`) and **interim**
        meetings (e.g. `interim-2026-aipref-05`). Returns every session the
        group has at that meeting (a WG can have two), each with the
        venue-**local** weekday/date and start–end time (converted from the
        agenda's UTC, DST-correct), the room, the Datatracker session id,
        and the agenda/minutes links. Numbered meetings add both Meetecho
        URLs (remote + onsite); interims have no onsite room, so they carry
        the agenda's free-text remote instructions (a Meetecho URL, a Teams
        note, …) instead.

        **Omit `meeting`** to list the group's upcoming meetings (numbered +
        interim) — the way to discover an interim id, which isn't guessable.

        Live (short TTL + freshness stamp; it reaches the network). Times
        are venue-local — never quote the UTC start as the local time.

        Args:
            corpus: The Working Group shortname (e.g. `httpbis`).
            meeting: A numbered meeting (`126`) or interim id
                (`interim-2026-aipref-05`); omit to list upcoming meetings.
        """
        return await _offload(tool_meeting_schedule, corpus, meeting)
