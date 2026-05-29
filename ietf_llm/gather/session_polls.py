"""Fetch IETF/IRTF session polls from Datatracker meeting materials.

IETF chairs record session polls — "raise of hands" or Meetecho-style
straw polls run during a meeting — as Datatracker documents with names
matching `polls-<meeting>-<wg>-<YYYYMMDDHHmm>-<version>`. They're not
formal consensus, but they signal where the room was leaning, and
they're useful to a consuming LLM answering "what direction is the
WG heading on X" questions.

The crawl piggybacks on the existing meeting materials hub walk in
`gather.meetings`: that page already enumerates all artefacts for a
session, so finding polls is one regex match away. No additional
Datatracker endpoint is involved — same request budget as before.

Each polls document we find is downloaded once and rendered to
`<wg>-polls-<meeting>-<datetime>.md` with the session datetime
recovered from the filename. The file is intentionally just the
cleaned HTML body of the document — we don't try to parse
individual poll questions and tallies, because their formatting
varies meeting-to-meeting and a consuming LLM reads them fine as
prose. Parsing is a follow-up if there's specific demand.

The timeline digest reads these files back and emits one event per
poll record, with kind="poll", linking to the file.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..paths import (
    DIR_MEETINGS,
    SUBDIR_POLLS,
    poll_path,
    polls_dir,
)
from ..utils import LogLevel, Verbosity, clean_html, fetch_resource, log

#: Datatracker materials URL pattern for poll records:
#:   /meeting/<N>/materials/polls-<N>-<wg>-<YYYYMMDDHHmm>-<version>
#: We anchor on the path component, not the full URL, so both
#: relative and absolute hrefs match.
_POLLS_HREF_RE = re.compile(
    r"/materials/polls-(\d+)-([a-z0-9_-]+)-(\d{12})-(\d+)",
    re.IGNORECASE,
)

#: Basename of a cached polls file: `<YYYYMMDDHHmm>.md`. Meeting code
#: comes from the path: `meetings/ietf<N>/polls/<basename>`.
_LOCAL_POLLS_BASENAME_RE = re.compile(
    r"^(?P<dt>\d{12})\.md$",
)


def fetch_polls_from_materials_page(
    materials_url: str,
    dest: str,
    wg: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Scan a meeting's materials hub for polls documents, download
    each, and write a cleaned markdown copy under `dest`.

    Returns the list of newly written file paths. A polls record that
    we've already cached (same filename present) is skipped, matching
    the meeting-minutes behaviour — re-runs are cheap.
    """
    log(
        f"Checking for poll records at {materials_url}...",
        verbose,
        level=LogLevel.PROGRESS,
    )
    res = fetch_resource(materials_url)
    if not res:
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    written: List[str] = []

    seen: set[str] = set()
    for anchor in soup.find_all("a"):
        href_attr = anchor.get("href") if isinstance(anchor, Tag) else None
        if not href_attr or isinstance(href_attr, list):
            continue
        href = str(href_attr)
        match = _POLLS_HREF_RE.search(href)
        if not match:
            continue
        meeting_num = match.group(1)
        # The doc's WG segment can differ in case from the URL we
        # were given (`/group/HTTPBIS/...` vs `polls-114-httpbis-...`).
        # Normalise to whatever the doc itself says — that's what
        # matches the rest of our cache layout.
        doc_wg = match.group(2).lower()
        if doc_wg != wg.lower():
            # Different WG's polls page rendered on the same materials
            # hub (rare but possible for joint sessions). Skip.
            continue
        session_dt = match.group(3)
        key = f"{meeting_num}-{session_dt}"
        if key in seen:
            continue  # version dedupe: take the first version listed
        seen.add(key)

        poll_url = urljoin(materials_url, href)
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

        content = _fetch_polls_content(poll_url, verbose)
        if not content:
            continue
        markdown = _render_polls_file(
            wg=wg,
            meeting_num=meeting_num,
            session_dt=session_dt,
            source_url=poll_url,
            body=content,
        )
        with open(path, "w", encoding="utf-8") as fh:
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


def _fetch_polls_content(url: str, verbose: Verbosity) -> Optional[str]:
    """Download the polls document and return its cleaned body, or None.

    Datatracker wraps polls content in the standard document viewer
    chrome. We grab the `.card-body` div when present (mirrors what
    `meetings._extract_minutes_content` does for minutes) and fall
    back to a full-page clean for older / variant layouts.
    """
    res = fetch_resource(url)
    if not res:
        return None
    # Already plain text or markdown? Pass through.
    ctype = res.headers.get("Content-Type", "").lower()
    if "text/markdown" in ctype or ctype.startswith("text/plain"):
        return str(res.text).strip()
    soup = BeautifulSoup(res.text, "html.parser")
    body_div = soup.find("div", class_="card-body")
    cleaned = clean_html(str(body_div)) if body_div else clean_html(res.text)
    text = str(cleaned).strip() if cleaned else ""
    return text or None


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
