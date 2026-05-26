"""Prepend a small context header to every transcript .md in the cache.

Transcripts are large (often 200-300 KB) and their content (raw
WEBVTT cues with speaker IDs and timestamps) carries no information
about which session they came from. Search hits deep in a transcript
land the agent on a quote with no idea whose meeting it's from.

We fix that by walking the cache, parsing each `*-transcript.md`
filename for date / WG / (sometimes) IETF meeting number, looking
up the companion minutes file when one exists, and prepending a
markdown frontmatter block. Idempotent: a sentinel comment on line
one signals "we've already done this file" so re-runs don't stack
headers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .utils import LogLevel, Verbosity, log


_SENTINEL = "<!-- ietf-llm:context-header -->"

# Two filename forms we recognise:
#   ietf-aipref-20260415-1315-transcript.md          (general / interim)
#   ietf125-aipref-20260316-0330-transcript.md       (specific IETF meeting)
_TRANSCRIPT_RE = re.compile(
    r"^(?P<prefix>ietf(?P<meeting_num>\d+)?)-"
    r"(?P<wg>[a-z0-9]+)-"
    r"(?P<date>\d{8})-"
    r"(?P<time>\d{4})-transcript\.md$",
    re.IGNORECASE,
)

_MEETING_DATE_RE = re.compile(r"^Date:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


@dataclass
class TranscriptContext:
    wg: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    meeting: Optional[str] = None  # e.g. "ietf125" or "interim2026aipref01"
    label: Optional[str] = None  # human label
    minutes_file: Optional[str] = None


def _meeting_label(meeting_code: str) -> str:
    match = re.match(r"^ietf(\d+)$", meeting_code, re.IGNORECASE)
    if match:
        return f"IETF {match.group(1)} meeting"
    match = re.match(
        r"^interim(\d{4})\w*?(\d+)$", meeting_code, re.IGNORECASE
    )
    if match:
        return f"Interim {match.group(1)} #{match.group(2)}"
    return meeting_code


def _build_date_index(cache_dir: str) -> Dict[str, str]:
    """Return YYYY-MM-DD → meeting-name for every minutes file in the cache."""
    index: Dict[str, str] = {}
    for name in os.listdir(cache_dir):
        if not (name.endswith("-minutes.md") or name.endswith("-minutes.txt")):
            continue
        meeting = name.rsplit("-minutes", 1)[0]
        try:
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as fh:
                head = fh.read(500)
        except OSError:
            continue
        match = _MEETING_DATE_RE.search(head)
        if match:
            index[match.group(1)] = meeting
    return index


def transcript_context(
    filename: str, cache_dir: str, date_index: Optional[Dict[str, str]] = None
) -> Optional[TranscriptContext]:
    """Infer (wg, date, meeting) for a transcript file.

    `date_index` is the result of `_build_date_index`; passed in as
    an optimisation so we don't re-scan minutes for every transcript.
    """
    match = _TRANSCRIPT_RE.match(filename)
    if not match:
        return None
    wg = match.group("wg").lower()
    raw_date = match.group("date")
    raw_time = match.group("time")
    date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    time_str = f"{raw_time[:2]}:{raw_time[2:4]}"

    ctx = TranscriptContext(wg=wg, date=date_str, time=time_str)

    # Direct meeting prefix wins (e.g. "ietf125-aipref-…").
    meeting_num = match.group("meeting_num")
    if meeting_num:
        ctx.meeting = f"ietf{meeting_num}"
        ctx.label = _meeting_label(ctx.meeting)
    else:
        # Fall back to date-matching against any minutes file we know.
        if date_index is None:
            date_index = _build_date_index(cache_dir)
        meeting = date_index.get(date_str)
        if meeting:
            ctx.meeting = meeting
            ctx.label = _meeting_label(meeting)

    if ctx.meeting:
        candidate = os.path.join(cache_dir, f"{ctx.meeting}-minutes.md")
        if os.path.isfile(candidate):
            ctx.minutes_file = os.path.basename(candidate)

    return ctx


def _render_header(filename: str, ctx: TranscriptContext) -> str:
    lines = [
        _SENTINEL,
        f"# {filename}",
        "",
        f"**Working Group:** {ctx.wg}",
        f"**Date / time:** {ctx.date} {ctx.time}",
    ]
    if ctx.label:
        lines.append(f"**Meeting:** {ctx.label}")
    elif ctx.meeting:
        lines.append(f"**Meeting:** {ctx.meeting}")
    if ctx.minutes_file:
        lines.append(f"**Minutes:** `{ctx.minutes_file}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def enrich_transcripts(
    cache_dir: str, verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Prepend a context header to every `*-transcript.md` that doesn't
    already have one. Returns the list of files modified."""
    if not os.path.isdir(cache_dir):
        return []
    date_index = _build_date_index(cache_dir)
    modified: List[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if not name.endswith("-transcript.md"):
            continue
        path = os.path.join(cache_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                first_chunk = fh.read(256)
        except OSError:
            continue
        if _SENTINEL in first_chunk:
            continue  # already enriched
        ctx = transcript_context(name, cache_dir, date_index=date_index)
        if ctx is None:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                body = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_render_header(name, ctx))
                fh.write("\n")
                fh.write(body)
        except OSError:
            continue
        modified.append(path)
    if modified:
        log(
            f"Enriched {len(modified)} transcript(s) with meeting context",
            verbose,
            level=LogLevel.STATUS,
        )
    return modified
