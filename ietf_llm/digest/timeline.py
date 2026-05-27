"""Build a chronological event log for a Working Group.

The events we surface answer "what happened, when" without forcing
an LLM to reconstruct chronology from per-file metadata. Signal
sources, each contributing dated events:

  - **Draft publications** — every `I-D Action: draft-…` thread is one
    publication event (date = the announcement's date).
  - **GitHub issues** — opened + closed events from the archive JSON.
  - **Meetings** — one event per session. Minutes / slides /
    transcripts are all aspects of the same meeting, so the event's
    line surfaces whichever artefacts exist (e.g. "minutes + 4 slide
    decks + transcript"). Sessions with only a transcript still
    appear, labelled by date when no minutes file matches.
  - **Session polls** — `<wg>-polls-<meeting>-<datetime>.md` files
    cached from Datatracker materials. Polls aren't formal consensus
    but they signal where a session was leaning.
  - **WG procedural milestones from the mailing list** — heuristic
    match on thread subjects (Working Group Last Call, Call for
    Adoption). Used as a fallback when Datatracker has no
    corresponding authoritative event.
  - **Datatracker governance** — charter approvals, chair
    appointments, group state changes, document adoption / IESG /
    RFC publication. Charter and chair events ignore the `--months`
    cutoff (foundational context); document events respect it.

Output: `<wg>-_timeline.md`. Events ordered most-recent-first within
each year, with years as `## YYYY` section headings so the agent
can grep down to the period it cares about.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..gather.datatracker_history import (
    fetch_doc_events,
    fetch_group_events,
    fetch_role_history,
)
from ..gather.mail_threads import Thread, build_threads, thread_slug
from ..gather.session_polls import discover_local_polls
from ..gather.transcript_context import transcript_context
from ..people import Registry
from ..utils import LogLevel, Verbosity, log
from .events import Event


# --- Sources ---------------------------------------------------------------


_ID_ACTION_RE = re.compile(
    r"^I-D Action:\s*(?P<name>draft-[A-Za-z0-9._-]+?)-(?P<ver>\d+)\.txt$",
    re.IGNORECASE,
)


def _draft_publications(threads: List[Thread]) -> List[Event]:
    """One Event per I-D Action announcement (per draft version)."""
    out: List[Event] = []
    for thread in threads:
        # Match against the normalised subject so the list prefix
        # ("[ai-control] " etc.) doesn't defeat the anchor.
        match = _ID_ACTION_RE.match(thread.subject.strip())
        if not match:
            continue
        when = thread.span[0] or thread.root.date
        if when is None:
            continue
        base = match.group("name")
        version = match.group("ver")
        out.append(
            Event(
                when=when,
                kind="draft-published",
                title=f"`{base}-{version}` published",
                link=_thread_link(thread),
            )
        )
    return out


_WGLC_RE = re.compile(
    r"(?:WG\s*Last\s*Call|Working\s*Group\s*Last\s*Call|WGLC)",
    re.IGNORECASE,
)
_ADOPT_RE = re.compile(r"call\s+for\s+adoption", re.IGNORECASE)


def _procedural_events(threads: List[Thread]) -> List[Event]:
    """WGLC and call-for-adoption thread starts."""
    out: List[Event] = []
    for thread in threads:
        # Use the normalised subject so the list prefix doesn't
        # interfere with the pattern matches.
        subject = thread.subject.strip()
        when = thread.span[0]
        if when is None:
            continue
        if _ADOPT_RE.search(subject):
            out.append(
                Event(
                    when=when,
                    kind="adoption-call",
                    title=f"Call for adoption thread starts: \"{subject}\"",
                    detail=(
                        f"{len(thread.members)} messages, "
                        f"{len(thread.participants)} participants"
                    ),
                    link=_thread_link(thread),
                )
            )
            continue
        if _WGLC_RE.search(subject):
            out.append(
                Event(
                    when=when,
                    kind="wglc",
                    title=f"WG Last Call thread: \"{subject}\"",
                    detail=(
                        f"{len(thread.members)} messages, "
                        f"{len(thread.participants)} participants"
                    ),
                    link=_thread_link(thread),
                )
            )
    return out


_MEETING_DATE_RE = re.compile(
    r"^Date:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE
)


@dataclass
class _Session:
    """One WG meeting session, with whichever artefacts we have on disk.

    A session may have minutes, slides, and/or a transcript — in any
    combination. The user's mental model is "the meeting" as a single
    event; minutes / slides / transcript are different aspects of it,
    not separate things to track on the timeline. So we collapse them
    into one event per session and surface all artefacts on its line.
    """

    when: datetime  # tz-aware UTC
    code: Optional[str]  # e.g. "ietf125", "interim2026aipref01"; None for orphans
    label: str  # human label: "IETF 125 meeting", "Interim 2026 #01", or fallback
    minutes_file: Optional[str] = None
    transcripts: List[str] = field(default_factory=list)
    slide_count: int = 0


def _meeting_events(cache_dir: str) -> List[Event]:
    """One event per WG session — meetings are the unit, with minutes /
    slides / transcript as aspects of that meeting rather than separate
    rows. A session needs at least one dated artefact (minutes Date:
    header, or transcript filename) to make the timeline; sessions with
    only slides are skipped (slides alone don't tell us when the
    session was).
    """
    sessions: Dict[str, _Session] = {}

    # 1. Seed sessions from minutes files. Each minutes file gives us
    # a definitive session date and a stable meeting code.
    for name in sorted(os.listdir(cache_dir)):
        if not (name.endswith("-minutes.md") or name.endswith("-minutes.txt")):
            continue
        path = os.path.join(cache_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                head = fh.read(500)
        except OSError:
            continue
        date_match = _MEETING_DATE_RE.search(head)
        if not date_match:
            continue
        try:
            when = datetime.strptime(date_match.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        when = when.replace(tzinfo=timezone.utc)
        code = name.rsplit("-minutes", 1)[0]
        sessions[code] = _Session(
            when=when,
            code=code,
            label=_meeting_label(name),
            minutes_file=name,
        )

    # 2. Attach slide counts. Slides share the meeting-code prefix,
    # and we already extract them as `<code>-slides-…pdf.txt`. We
    # don't list every slide deck individually on the timeline —
    # a count is enough; the consumer can `list_files` if they want
    # the full set.
    for name in os.listdir(cache_dir):
        if not name.endswith(".pdf.txt"):
            continue
        # Match `<code>-slides-...` where `<code>` is a known session.
        # The code is the part before the first `-slides-`.
        slides_marker = "-slides-"
        marker_pos = name.find(slides_marker)
        if marker_pos == -1:
            continue
        code = name[:marker_pos]
        if code in sessions:
            sessions[code].slide_count += 1

    # 3. Attach transcripts. Two pathways:
    # (a) Transcript filename carries a meeting prefix ("ietf125-…") →
    #     attach to that session directly.
    # (b) Generic prefix ("ietf-aipref-…") → `transcript_context`
    #     matches by date against minutes files; attach if matched.
    # Anything that fails both becomes an orphan session — emitted with
    # a date-only label so the consumer at least sees the transcript
    # exists.
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith("-transcript.md"):
            continue
        ctx = transcript_context(name, cache_dir)
        if ctx is None:
            continue
        transcript_when = _transcript_datetime(ctx.date, ctx.time)
        if transcript_when is None:
            continue
        if ctx.meeting and ctx.meeting in sessions:
            sessions[ctx.meeting].transcripts.append(name)
            continue
        # Orphan: no minutes file matched. Use the transcript's own
        # datetime as the session key so multiple orphans don't collide.
        key = f"_orphan:{ctx.date}-{ctx.time}"
        if key not in sessions:
            sessions[key] = _Session(
                when=transcript_when,
                code=None,
                label=ctx.label or f"{ctx.wg} session ({ctx.date} {ctx.time} UTC)",
                minutes_file=None,
            )
        sessions[key].transcripts.append(name)

    # 4. Emit one Event per session, with all artefacts rendered on
    # the line. Order doesn't matter here — build_events sorts.
    return [_session_event(session) for session in sessions.values()]


def _session_event(session: _Session) -> Event:
    """Render a session as a single Event with all its artefacts."""
    parts: List[str] = []
    if session.minutes_file:
        parts.append(f"minutes `{session.minutes_file}`")
    for transcript in session.transcripts:
        parts.append(f"transcript `{transcript}`")
    if session.slide_count:
        word = "deck" if session.slide_count == 1 else "decks"
        parts.append(f"{session.slide_count} slide {word}")
    link = " · ".join(parts) if parts else None
    return Event(
        when=session.when,
        kind="meeting",
        title=session.label,
        # No detail field: the artefacts list IS the detail. Keeping
        # detail empty avoids a redundant "— …" segment in the render.
        detail=None,
        link=link,
    )


def _transcript_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """Combine the transcript-context's date + time strings into a
    tz-aware UTC datetime. Returns None if either is malformed."""
    try:
        parsed = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _meeting_label(filename: str) -> str:
    """Render a short meeting label from its filename.

    `ietf124-minutes.md` → "IETF 124 meeting"
    `interim2025aipref09-minutes.md` → "Interim 2025 #09"
    """
    base = filename.rsplit("-minutes", 1)[0]
    ietf_match = re.match(r"^ietf(\d+)$", base, re.IGNORECASE)
    if ietf_match:
        return f"IETF {ietf_match.group(1)} meeting"
    interim_match = re.match(
        r"^interim(\d{4})\w*?(\d+)$", base, re.IGNORECASE
    )
    if interim_match:
        return (
            f"Interim {interim_match.group(1)} #{interim_match.group(2)}"
        )
    return base


def _issue_events(cache_dir: str, wg: str, registry: Registry) -> List[Event]:
    """Opened / closed events from the GitHub archive JSON files.

    Closure date is taken from `updatedAt` when state is closed (the
    archive doesn't record closedAt explicitly). Approximate but
    consistent.
    """
    out: List[Event] = []
    for name in os.listdir(cache_dir):
        if not (name.startswith(f"{wg}-github-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        repo = data.get("repo", "")
        for issue in data.get("issues") or []:
            number = issue.get("number")
            title = (issue.get("title") or "").strip()
            author = issue.get("author") or ""
            canonical_author = registry.canonical_for_github(author) or author
            created = _parse_iso(issue.get("createdAt"))
            if created:
                out.append(
                    Event(
                        when=created,
                        kind="issue-opened",
                        title=(
                            f"Issue #{number} opened: \"{title}\""
                        ),
                        detail=f"by {canonical_author} ({repo})",
                    )
                )
            if (issue.get("state") or "").lower() == "closed":
                closed = _parse_iso(issue.get("updatedAt"))
                if closed:
                    out.append(
                        Event(
                            when=closed,
                            kind="issue-closed",
                            title=f"Issue #{number} closed: \"{title}\"",
                            detail=f"({repo})",
                        )
                    )
    return out


def _poll_events(cache_dir: str) -> List[Event]:
    """One event per cached session-polls file.

    The session datetime is in the filename (set by Datatracker's
    naming convention), so we don't have to peek inside the file —
    a directory scan is enough. The link points at the local file
    so a consuming LLM can read the full poll record with one
    `read_file_section` call.
    """
    out: List[Event] = []
    for poll in discover_local_polls(cache_dir):
        out.append(
            Event(
                when=poll.when,
                kind="poll",
                title=f"Session polls recorded at IETF {poll.meeting}",
                detail=None,
                link=f"`{poll.filename}`",
            )
        )
    return out


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _thread_link(thread: Thread) -> str:
    first = thread.span[0]
    iso = first.strftime("%Y-%m-%d") if first else None
    return f"`<wg>-thread-{thread_slug(thread.root.subject, iso)}.md`"


# --- Public entry point ----------------------------------------------------


def build_events(
    wg: str,
    cache_dir: str,
    registry: Registry,
    months: int = 12,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[Event]:
    """Collect events from every source and return them sorted desc.

    `months` bounds the Datatracker document-event window; group-level
    events (charter, chair appointments) ignore it per policy. The
    mailing-list-derived events are implicitly bounded by whatever
    `--months` was used when the mail was fetched.
    """
    threads = build_threads(wg, registry=registry)
    events: List[Event] = []
    events.extend(_draft_publications(threads))
    events.extend(_meeting_events(cache_dir))
    events.extend(_poll_events(cache_dir))
    events.extend(_issue_events(cache_dir, wg, registry))

    # Datatracker is the authoritative source for governance and
    # document-lifecycle events. If those calls succeed we prefer them
    # over the mail-subject heuristic for WGLC / adoption.
    dt_events: List[Event] = []
    dt_events.extend(fetch_group_events(wg, months, verbose))
    dt_events.extend(fetch_role_history(wg, verbose))
    dt_events.extend(fetch_doc_events(wg, months, verbose))
    events.extend(dt_events)

    # Heuristic WGLC / adoption from mailing list subjects. Datatracker
    # ought to cover these authoritatively, but we keep the fallback for
    # WGs whose Datatracker history is sparse or unavailable: a
    # heuristic-derived event is better than no event. The merge is
    # additive — if Datatracker AND the subject heuristic both fire on
    # the same procedural moment, the digest just shows both rows, which
    # is informative rather than wrong.
    events.extend(_procedural_events(threads))

    events.sort(key=lambda e: e.when, reverse=True)
    return events


def write_timeline_digest(
    wg: str,
    cache_dir: str,
    registry: Registry,
    months: int = 12,
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Render `<wg>-_timeline.md`. Returns the file path, or None if empty.

    The link column substitutes the actual WG acronym so the file
    references resolve against the cache. `months` bounds the
    Datatracker document-event window (governance events always
    appear, regardless).
    """
    events = build_events(wg, cache_dir, registry, months=months, verbose=verbose)
    if not events:
        return None

    by_year: Dict[int, List[Event]] = {}
    for event in events:
        by_year.setdefault(event.when.year, []).append(event)

    out_path = os.path.join(cache_dir, f"{wg}-_timeline.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: timeline\n\n")
        fh.write(
            f"_{len(events)} dated events across {len(by_year)} year(s) — "
            "drafts published, issues opened and closed, meetings held "
            "(with minutes / slides / transcripts bundled as aspects of "
            "the same session), session polls recorded, charter "
            "approvals and chair appointments (Datatracker), document "
            "adoption / IESG / RFC publication (Datatracker), WGLCs "
            "and adoption calls (Datatracker when available, "
            "mailing-list subject-line heuristic as fallback). "
            "Newest first within each year._\n\n"
        )
        for year in sorted(by_year, reverse=True):
            fh.write(f"## {year}\n\n")
            for event in by_year[year]:
                date_str = event.when.strftime("%Y-%m-%d")
                line = f"- **{date_str}** — {event.title}"
                if event.detail:
                    line += f" — {event.detail}"
                if event.link:
                    # Resolve <wg> placeholder for thread links.
                    line += f" · {event.link.replace('<wg>', wg)}"
                fh.write(line + "\n")
            fh.write("\n")

    log(
        f"Wrote timeline: {len(events)} events",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path
