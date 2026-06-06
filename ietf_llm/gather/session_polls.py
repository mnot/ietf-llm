"""Fetch IETF/IRTF session polls from the Datatracker polls doctype.

IETF chairs record session polls — "raise of hands" or Meetecho-style
straw polls run during a meeting — as Datatracker documents named
`polls-<meeting>-<wg>-<YYYYMMDDHHmm>`. They're not formal consensus,
but they signal where the room was leaning, and they're useful to a
consuming LLM answering "what direction is the WG heading on X"
questions.

Polls are gathered directly by group via the document API
(`type=polls`), independent of the meeting materials walk. Each poll
record is a JSON array of questions with raise-hand tallies; we fetch
it, render it to markdown, and write it to
`meetings/ietf<meeting>/polls/<YYYYMMDDHHmm>.md`.

The timeline digest reads these files back and emits one event per
poll record, with kind="poll", linking to the file.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import List, Optional

from ..paths import (
    DIR_MEETINGS,
    SUBDIR_POLLS,
    poll_path,
    polls_dir,
)
from ..utils import LogLevel, Verbosity, atomic_open, fetch_resource, log
from .datatracker import iter_group_documents

#: Datatracker polls document name: `polls-<meeting>-<wg>-<YYYYMMDDHHmm>`.
#: Only numbered meetings are matched (interim polls are rare and were
#: never gathered by the previous implementation either).
_POLLS_NAME_RE = re.compile(r"^polls-(\d+)-[a-z0-9_-]+-(\d{12})$", re.IGNORECASE)

#: Basename of a cached polls file: `<YYYYMMDDHHmm>.md`. Meeting code
#: comes from the path: `meetings/ietf<N>/polls/<basename>`.
_LOCAL_POLLS_BASENAME_RE = re.compile(
    r"^(?P<dt>\d{12})\.md$",
)


def process_session_polls(
    wg: str,
    dest: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Gather a WG's session polls via the Datatracker polls doctype.

    Walks `/api/v1/doc/document/?group__acronym=<wg>&type=polls`, fetches
    each poll record (a JSON array of questions + raise-hand tallies),
    renders it to markdown, and writes it under
    `meetings/ietf<meeting>/polls/<YYYYMMDDHHmm>.md`. Polls already
    cached (same filename present) are skipped, so re-runs are cheap.
    Returns the list of newly written file paths.
    """
    written: List[str] = []
    for doc in iter_group_documents(wg, "polls"):
        name = str(doc.get("name") or "")
        match = _POLLS_NAME_RE.match(name)
        if not match:
            continue
        meeting_num, session_dt = match.group(1), match.group(2)
        code = f"ietf{meeting_num}"
        os.makedirs(polls_dir(dest, code), exist_ok=True)
        path = poll_path(dest, code, session_dt)
        if os.path.exists(path):
            log(
                f"Skipping polls {os.path.relpath(path, dest)}: already cached.",
                verbose,
                level=LogLevel.PROGRESS,
            )
            continue

        poll_url = (
            f"https://datatracker.ietf.org/meeting/{meeting_num}/materials/{name}"
        )
        res = fetch_resource(poll_url)
        if not res:
            continue
        body = _render_polls_body(str(res.text))
        if not body:
            continue
        markdown = _render_polls_file(
            wg=wg,
            meeting_num=meeting_num,
            session_dt=session_dt,
            source_url=poll_url,
            body=body,
        )
        with atomic_open(path) as fh:
            fh.write(markdown)
        log(
            f"Wrote {os.path.relpath(path, dest)}",
            verbose,
            level=LogLevel.PROGRESS,
        )
        written.append(path)

    return written


def discover_local_polls(cache_dir: str) -> List["LocalPoll"]:
    """Walk `meetings/<code>/polls/*.md` and return every cached polls
    record. Used by the timeline digest to emit `poll` events without
    re-fetching anything.
    """
    out: List[LocalPoll] = []
    meetings_root = os.path.join(cache_dir, DIR_MEETINGS)
    if not os.path.isdir(meetings_root):
        return out
    for code in sorted(os.listdir(meetings_root)):
        polls_subdir = os.path.join(meetings_root, code, SUBDIR_POLLS)
        if not os.path.isdir(polls_subdir):
            continue
        # Extract the numeric meeting number from codes like "ietf114"
        # (the LocalPoll model carries it as the display value); for
        # interims we just use the code.
        meeting_label = code[len("ietf") :] if code.startswith("ietf") else code
        for name in sorted(os.listdir(polls_subdir)):
            match = _LOCAL_POLLS_BASENAME_RE.match(name)
            if not match:
                continue
            when = _parse_session_datetime(match.group("dt"))
            if when is None:
                continue
            relpath = os.path.relpath(
                os.path.join(polls_subdir, name),
                cache_dir,
            )
            out.append(
                LocalPoll(
                    filename=relpath,
                    wg="",  # WG is implicit in the cache root
                    meeting=meeting_label,
                    when=when,
                )
            )
    return out


# --- Internal helpers -----------------------------------------------------


class LocalPoll:
    """Minimal record for the timeline integration — no extra dataclass
    surface to maintain. The file's contents stay on disk; this just
    points at it with enough metadata to emit a timeline event."""

    __slots__ = ("filename", "wg", "meeting", "when")

    def __init__(self, filename: str, wg: str, meeting: str, when: datetime) -> None:
        self.filename = filename
        self.wg = wg
        self.meeting = meeting
        self.when = when


def _render_polls_body(raw: str) -> Optional[str]:
    """Render the polls JSON array into readable markdown — one block per
    question with its raise-hand tallies. Falls back to the raw payload
    (stripped) if it isn't the expected JSON shape, so we never silently
    drop a poll record whose format we don't recognise."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw.strip() or None
    if not isinstance(data, list) or not data:
        return raw.strip() or None

    blocks: List[str] = []
    for idx, poll in enumerate(data, 1):
        if not isinstance(poll, dict):
            continue
        question = str(poll.get("text") or "").strip()
        blocks.append(f"### Poll {idx}: {question}" if question else f"### Poll {idx}")
        for key, value in poll.items():
            # Skip the question text and the timestamps; surface the
            # numeric tallies (raise_hand, do_not_raise_hand, …).
            if key in ("text", "start_time", "end_time"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            label = key.replace("_", " ").capitalize()
            blocks.append(f"- {label}: {value}")
        blocks.append("")
    rendered = "\n".join(blocks).strip()
    return rendered or None


def _render_polls_file(
    *,
    wg: str,
    meeting_num: str,
    session_dt: str,
    source_url: str,
    body: str,
) -> str:
    """Compose the on-disk markdown file. The header mirrors the
    per-thread / per-issue file shape so the same chunker fallback
    (windowed) handles them with sensible titles.
    """
    pretty_dt = _format_session_datetime(session_dt)
    return (
        f"# {wg} session polls — IETF {meeting_num}\n\n"
        f"**Meeting:** IETF {meeting_num}  \n"
        f"**Session start:** {pretty_dt}  \n"
        f"**Working Group:** {wg}  \n"
        f"**Source:** {source_url}  \n\n"
        "_Recorded during the session. Polls indicate where the room is "
        "leaning, not formal WG consensus — that goes through WGLC and "
        "chair declaration._\n\n"
        "---\n\n"
        f"{body}\n"
    )


def _parse_session_datetime(token: str) -> Optional[datetime]:
    """`YYYYMMDDHHmm` → tz-aware UTC datetime. Returns None if the
    token doesn't parse (e.g. truncated filename, unexpected format)."""
    if len(token) != 12 or not token.isdigit():
        return None
    try:
        parsed = datetime.strptime(token, "%Y%m%d%H%M")
    except ValueError:
        return None
    # Datatracker stores meeting times in UTC; attach explicitly so
    # the timeline sorter doesn't trip over naive vs aware comparisons.
    return parsed.replace(tzinfo=timezone.utc)


def _format_session_datetime(token: str) -> str:
    """Human-readable form for the file header. Falls back to the
    raw token if parsing fails — we never want a friendly format to
    swallow data on a weird input."""
    when = _parse_session_datetime(token)
    return when.strftime("%Y-%m-%d %H:%M UTC") if when else token
