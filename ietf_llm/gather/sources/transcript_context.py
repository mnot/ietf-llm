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

from ...log import LogLevel, Verbosity, log
from ...paths import (
    DIR_MEETINGS,
    SUBDIR_TRANSCRIPTS,
    is_transcript_relpath,
    meeting_code_for_relpath,
    meeting_label,
    minutes_path,
)

_SENTINEL = "<!-- ietf-llm:context-header -->"

# Post-reorg layout: transcripts live at
# `meetings/<code>/transcripts/<YYYYMMDDHHmm>.md` (or under
# `meetings/_orphans/transcripts/…` when there's no matching meeting
# code). The basename is the session datetime; the meeting code
# comes from the path.
_TRANSCRIPT_BASENAME_RE = re.compile(
    r"^(?P<date>\d{8})(?P<time>\d{4})\.md$",
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
    # Shared parser in paths.py — handles ietf<N>, date-coded
    # clustered interims, and legacy per-session interim codes.
    return meeting_label(meeting_code)


def _build_date_index(cache_dir: str) -> Dict[str, str]:
    """Return YYYY-MM-DD → meeting-code for every minutes file in the cache.

    Walks `meetings/<code>/minutes.md` recursively (post-reorg layout).
    """
    index: Dict[str, str] = {}
    meetings_root = os.path.join(cache_dir, DIR_MEETINGS)
    if not os.path.isdir(meetings_root):
        return index
    for code in os.listdir(meetings_root):
        candidate = minutes_path(cache_dir, code)
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                head = fh.read(500)
        except OSError:
            continue
        match = _MEETING_DATE_RE.search(head)
        if match:
            index[match.group(1)] = code
    return index


def transcript_context(
    relpath: str,
    cache_dir: str,
    date_index: Optional[Dict[str, str]] = None,
    wg: Optional[str] = None,
) -> Optional[TranscriptContext]:
    """Infer (wg, date, meeting) for a transcript file.

    `relpath` is the path relative to cache_dir, e.g.
    `meetings/ietf125/transcripts/202603160330.md`. The meeting code
    is read directly from the path; the date/time come from the
    basename. `wg` is the WG shortname — passed in by callers that
    know it (the gather pipeline) since post-reorg filenames don't
    carry it.

    `date_index` is the result of `_build_date_index`; passed in as
    an optimisation so we don't re-scan minutes for every transcript.
    """
    if not is_transcript_relpath(relpath):
        return None
    basename = os.path.basename(relpath)
    match = _TRANSCRIPT_BASENAME_RE.match(basename)
    if not match:
        return None
    raw_date = match.group("date")
    raw_time = match.group("time")
    date_str = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    time_str = f"{raw_time[:2]}:{raw_time[2:4]}"

    ctx = TranscriptContext(wg=wg or "", date=date_str, time=time_str)

    # Meeting code comes from the path (post-reorg). "_orphans" is the
    # synthetic dir for transcripts without a matching meeting code.
    path_code = meeting_code_for_relpath(relpath)
    if path_code and path_code != "_orphans":
        ctx.meeting = path_code
        ctx.label = _meeting_label(path_code)
    else:
        # Orphan: fall back to date-matching against minutes we know.
        if date_index is None:
            date_index = _build_date_index(cache_dir)
        meeting = date_index.get(date_str)
        if meeting:
            ctx.meeting = meeting
            ctx.label = _meeting_label(meeting)

    if ctx.meeting:
        candidate = minutes_path(cache_dir, ctx.meeting)
        if os.path.isfile(candidate):
            ctx.minutes_file = os.path.relpath(candidate, cache_dir)

    return ctx


# Labels the context header emits as `**Label:**` lines. They share the
# `**Name:**` shape of transcript speaker cues, so `people.meetings` excludes
# them when parsing speakers — it imports `CONTEXT_HEADER_LABELS` (below) so
# the two stay in lock-step. `_render_header` builds its lines from these same
# constants, so renaming a label here forces the exclusion set to follow.
_LABEL_WG = "Working Group"
_LABEL_DATETIME = "Date / time"
_LABEL_MEETING = "Meeting"
_LABEL_MINUTES = "Minutes"
CONTEXT_HEADER_LABELS = frozenset(
    label.lower()
    for label in (_LABEL_WG, _LABEL_DATETIME, _LABEL_MEETING, _LABEL_MINUTES)
)


def _render_header(filename: str, ctx: TranscriptContext) -> str:
    lines = [
        _SENTINEL,
        f"# {filename}",
        "",
        f"**{_LABEL_WG}:** {ctx.wg}",
        f"**{_LABEL_DATETIME}:** {ctx.date} {ctx.time}",
    ]
    if ctx.label:
        lines.append(f"**{_LABEL_MEETING}:** {ctx.label}")
    elif ctx.meeting:
        lines.append(f"**{_LABEL_MEETING}:** {ctx.meeting}")
    if ctx.minutes_file:
        lines.append(f"**{_LABEL_MINUTES}:** `{ctx.minutes_file}`")
    lines.append("")
    return "\n".join(lines) + "\n"


def enrich_transcripts(
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
    wg: Optional[str] = None,
) -> List[str]:
    """Prepend a context header to every transcript that doesn't already
    have one. Walks `meetings/<code>/transcripts/*.md` recursively.
    Returns the list of files modified.
    """
    if not os.path.isdir(cache_dir):
        return []
    date_index = _build_date_index(cache_dir)
    modified: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        # Only walk transcripts subdirs to keep this cheap.
        if not dirpath.endswith(f"/{SUBDIR_TRANSCRIPTS}"):
            continue
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            relpath = os.path.relpath(path, cache_dir)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    first_chunk = fh.read(256)
            except OSError:
                continue
            if _SENTINEL in first_chunk:
                continue  # already enriched
            ctx = transcript_context(
                relpath,
                cache_dir,
                date_index=date_index,
                wg=wg,
            )
            if ctx is None:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    body = fh.read()
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(_render_header(relpath, ctx))
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
