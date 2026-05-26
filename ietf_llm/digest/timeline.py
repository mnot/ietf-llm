"""Build a chronological event log for a Working Group.

The events we surface answer "what happened, when" without forcing
an LLM to reconstruct chronology from per-file metadata. Four signal
sources, each contributing dated events:

  - **Draft publications** — every `I-D Action: draft-…` thread is one
    publication event (date = the announcement's date).
  - **GitHub issues** — opened + closed events from the archive JSON.
  - **Meetings** — minutes files carry a `Date:` line on row 2.
  - **WG procedural milestones** — heuristic match on thread subjects:
    Working Group Last Call, Call for Adoption, etc.

Output: `<wg>-_timeline.md`. Events ordered most-recent-first within
each year, with years as `## YYYY` section headings so the agent
can grep down to the period it cares about.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..gather.mail_threads import Thread, build_threads, thread_slug
from ..people import Registry
from ..utils import LogLevel, Verbosity, log


@dataclass
class Event:
    when: datetime  # tz-aware UTC
    kind: str  # short slug: draft-published, issue-opened, …
    title: str  # one-line description for the digest row
    detail: Optional[str] = None  # optional second-line context
    link: Optional[str] = None  # filename to point readers at


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


def _meeting_events(cache_dir: str) -> List[Event]:
    """Each `*-minutes.md` file with a Date: line in its header."""
    out: List[Event] = []
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
        label = _meeting_label(name)
        out.append(
            Event(
                when=when,
                kind="meeting",
                title=f"{label} held",
                link=f"`{name}`",
            )
        )
    return out


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
    wg: str, cache_dir: str, registry: Registry
) -> List[Event]:
    """Collect events from every source and return them sorted desc."""
    threads = build_threads(wg, registry=registry)
    events: List[Event] = []
    events.extend(_draft_publications(threads))
    events.extend(_procedural_events(threads))
    events.extend(_meeting_events(cache_dir))
    events.extend(_issue_events(cache_dir, wg, registry))
    events.sort(key=lambda e: e.when, reverse=True)
    return events


def write_timeline_digest(
    wg: str,
    cache_dir: str,
    registry: Registry,
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Render `<wg>-_timeline.md`. Returns the file path, or None if empty.

    The link column substitutes the actual WG acronym so the file
    references resolve against the cache.
    """
    events = build_events(wg, cache_dir, registry)
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
            "drafts published, issues opened and closed, meetings held, "
            "WGLCs and adoption calls. Newest first within each year. "
            "Heuristic for procedural events: thread-subject pattern "
            "matches, so phrasing variations may be missed._\n\n"
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
