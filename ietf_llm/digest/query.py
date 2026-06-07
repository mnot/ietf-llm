"""Filtered + paginated reads over the existing markdown digests.

The digest files (`<wg>-_issues.md`, `<wg>-_threads.md`, etc.) are
already written as markdown tables. Rather than maintain a second
serialisation, we parse the tables back into rows on demand and
apply the filters / limits the agent asked for.

That keeps the contract clean — the on-disk format never changes,
and an agent calling `read_digest(wg, kind)` with no filters gets
the same bytes as before. With filters, it gets a re-rendered
markdown subset.

Per-kind filter semantics:

  issues   state="open"|"closed"   limit=N   label="<substr>"
                                  author="<substr>"
  threads  since="YYYY-MM-DD"      limit=N   min_messages=N
           until="YYYY-MM-DD"
  people   role="<substr>"         limit=N   min_messages=N
  timeline since/until=...         limit=N   kind="<event-kind>"

Filters that don't apply to a given digest kind are silently
ignored (so the same call shape works across kinds).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Section:
    """One markdown table from a digest, with its preceding heading."""

    heading: str  # e.g. "## fooorg/bar"
    columns: List[str]
    rows: List[List[str]]


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _split_row(line: str) -> List[str]:
    """Split a markdown table row into trimmed cell values."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_separator(cells: List[str]) -> bool:
    return all(set(cell) <= set("-: ") and cell for cell in cells)


def parse_md_tables(text: str) -> List[Section]:
    """Pull (heading, columns, rows) for every markdown table in `text`.

    Robust to interleaved prose, multiple tables under one heading,
    and tables without an explicit heading (those get heading="").
    """
    sections: List[Section] = []
    current_heading = ""
    columns: Optional[List[str]] = None
    rows: List[List[str]] = []
    pending_header_row: Optional[List[str]] = None

    def _flush() -> None:
        nonlocal columns, rows, pending_header_row
        if columns and rows:
            sections.append(Section(current_heading, columns, rows))
        columns = None
        rows = []
        pending_header_row = None

    for line in text.splitlines():
        head_match = _HEADING_RE.match(line)
        if head_match:
            _flush()
            current_heading = line.strip()
            continue
        if not line.strip():
            _flush()
            continue
        if line.lstrip().startswith("|"):
            cells = _split_row(line)
            if pending_header_row is None and columns is None:
                # First row of a table candidate; need to see the
                # separator on the next non-empty line to confirm.
                pending_header_row = cells
                continue
            if pending_header_row is not None and columns is None:
                if _is_separator(cells):
                    columns = pending_header_row
                    pending_header_row = None
                else:
                    # Not actually a table — treat both rows as prose
                    # and reset.
                    pending_header_row = None
                continue
            if columns is not None:
                if _is_separator(cells):
                    continue  # tolerate stray separators
                rows.append(cells)
        else:
            # Non-table, non-heading line; if we were mid-table, flush.
            _flush()

    _flush()
    return sections


# --- Filter predicates per kind --------------------------------------------


def _idx(columns: List[str], name: str) -> Optional[int]:
    """Case-insensitive lookup of a column index by name."""
    lower = [c.lower() for c in columns]
    try:
        return lower.index(name.lower())
    except ValueError:
        return None


def _row_field(row: List[str], columns: List[str], name: str) -> str:
    pos = _idx(columns, name)
    if pos is None or pos >= len(row):
        return ""
    return row[pos]


def _parse_date_cell(value: str) -> str:
    """Extract a YYYY-MM-DD prefix from a cell that might have extra text."""
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return match.group(0) if match else ""


def is_idaction_publication(rest: str) -> bool:
    """An automated I-D Action draft-publication timeline event
    (`… published · threads/…`)."""
    return " published · " in rest


def is_ballot_position(rest: str) -> bool:
    """An individual IESG ballot-position timeline event (`… → …`)."""
    return " → " in rest


def is_mechanical_timeline_event(rest: str) -> bool:
    """True for a routine, machine-shaped timeline event — an I-D Action
    publication or a single IESG ballot position. Lets callers fold or
    exclude them so human discussion / decision events stand out. One
    definition, shared by the timeline `exclude_mechanical` filter and the
    overview recent-activity fold."""
    return is_idaction_publication(rest) or is_ballot_position(rest)


#: Which column carries the per-row "activity" count, by digest kind.
_ACTIVITY_COLUMN = {"threads": "msgs", "issues": "comments"}


def _activity_count(row: List[str], columns: List[str], kind: str) -> int:
    """The activity count for `sort="activity"` (thread messages / issue
    comments), or -1 when absent so it sorts last."""
    col = _ACTIVITY_COLUMN.get(kind)
    if col is None:
        return -1
    match = re.search(r"\d+", _row_field(row, columns, col))
    return int(match.group(0)) if match else -1


def filter_rows(section: Section, kind: str, filters: Dict[str, Any]) -> Section:
    """Return a new Section with rows filtered per kind-specific rules."""
    keep: List[List[str]] = []
    for row in section.rows:
        if not _row_matches(row, section.columns, kind, filters):
            continue
        keep.append(row)
    # `sort="activity"` ranks by message / comment count (heat) instead of
    # the digest's default recency order — applied before the limit so the
    # top-N are the busiest, not the newest. Stable, so ties keep recency.
    if filters.get("sort") == "activity":
        keep = sorted(
            keep,
            key=lambda r: _activity_count(r, section.columns, kind),
            reverse=True,
        )
    limit = filters.get("limit")
    if isinstance(limit, int) and limit >= 0:
        keep = keep[:limit]
    return Section(section.heading, section.columns, keep)


def _row_matches(  # pylint: disable=too-many-return-statements
    row: List[str], columns: List[str], kind: str, filters: Dict[str, Any]
) -> bool:
    if kind == "issues":
        state = filters.get("state")
        if state:
            cell = _row_field(row, columns, "state").lower()
            if cell != state.lower():
                return False
        label = filters.get("label")
        if label and label not in _row_field(row, columns, "labels").lower():
            return False
        author = filters.get("author")
        if author and author.lower() not in _row_field(row, columns, "author").lower():
            return False
    elif kind == "threads":
        since = filters.get("since")
        until = filters.get("until")
        last = _parse_date_cell(_row_field(row, columns, "last"))
        if since and (not last or last < since):
            return False
        if until and (not last or last > until):
            return False
        min_msgs = filters.get("min_messages")
        if isinstance(min_msgs, int):
            count_cell = _row_field(row, columns, "msgs") or _row_field(
                row, columns, "messages"
            )
            try:
                if int(count_cell) < min_msgs:
                    return False
            except ValueError:
                return False
        # Subject substring filter — case-insensitive. The headline use
        # case is WGs that don't tag GitHub issues but cluster topics
        # via mailing-list subject lines (TLS does this with `[mlkem]`,
        # `[ech]`, etc.). `read_digest("threads", subject="mlkem")`
        # then surfaces every thread whose subject contains that token.
        subject_filter = filters.get("subject")
        if subject_filter:
            if (
                subject_filter.lower()
                not in _row_field(row, columns, "subject").lower()
            ):
                return False
    elif kind == "people":
        role = filters.get("role")
        if role and role.lower() not in _row_field(row, columns, "roles").lower():
            return False
        min_msgs = filters.get("min_messages")
        if isinstance(min_msgs, int):
            count_cell = _row_field(row, columns, "msgs") or _row_field(
                row, columns, "messages"
            )
            try:
                if int(count_cell) < min_msgs:
                    return False
            except ValueError:
                # No msgs column on this table (e.g. leadership) → exclude
                return False
    elif kind == "timeline":
        # Timeline rows aren't tables — they're bullet lines. Filtering
        # is handled in query_digest below.
        return True
    return True


def render_section(section: Section) -> str:
    """Render a Section back to markdown."""
    out: List[str] = []
    if section.heading:
        out.append(section.heading)
        out.append("")
    if not section.columns or not section.rows:
        return "\n".join(out) + "\n"
    out.append("| " + " | ".join(section.columns) + " |")
    out.append("|" + "|".join("---" for _ in section.columns) + "|")
    for row in section.rows:
        # Pad or trim to match column count, defensively.
        padded = (row + [""] * len(section.columns))[: len(section.columns)]
        out.append("| " + " | ".join(padded) + " |")
    out.append("")
    return "\n".join(out) + "\n"


# --- Timeline (bullet-list, not table) -------------------------------------


_TIMELINE_BULLET_RE = re.compile(
    r"^- \*\*(?P<date>\d{4}-\d{2}-\d{2})\*\* — (?P<rest>.+)$"
)


def _filter_timeline(text: str, filters: Dict[str, Any]) -> str:
    """Apply since / until / kind / limit to a timeline digest body.

    Timeline rows are bullets like
        - **2026-04-19** — Issue #160 closed: "…"
    grouped under `## YYYY` headings. We keep matching bullets and
    drop headings that end up empty.
    """
    since = filters.get("since")
    until = filters.get("until")
    event_kind = filters.get("event_kind")
    exclude_mechanical = filters.get("exclude_mechanical")
    limit = filters.get("limit")

    lines: List[str] = []
    kept_per_year: Dict[str, List[str]] = {}
    current_year: Optional[str] = None
    preamble_done = False
    preamble: List[str] = []

    for line in text.splitlines():
        match = re.match(r"^## (\d{4})\s*$", line)
        if match:
            current_year = match.group(1)
            kept_per_year.setdefault(current_year, [])
            preamble_done = True
            continue
        if not preamble_done:
            preamble.append(line)
            continue
        if current_year is None:
            continue
        bullet = _TIMELINE_BULLET_RE.match(line)
        if not bullet:
            continue
        date = bullet.group("date")
        rest = bullet.group("rest")
        if since and date < since:
            continue
        if until and date > until:
            continue
        if exclude_mechanical and is_mechanical_timeline_event(rest):
            continue
        if event_kind:
            # Map our event kinds to substrings that distinguish them.
            kind_markers = {
                "draft-published": "published",
                "issue-opened": "opened",
                "issue-closed": "closed",
                # The timeline writer renders a session as its label
                # ("IETF 125 meeting"), not "meeting held" — the old marker
                # matched nothing. "meeting" catches IETF sessions; interim
                # labels ("Interim 2026 #01") carry no common token, so those
                # are not reliably matchable via substring.
                "meeting": "meeting",
                "wglc": "Last Call",
                "adoption-call": "adoption",
            }
            marker = kind_markers.get(event_kind, event_kind)
            if marker.lower() not in rest.lower():
                continue
        kept_per_year[current_year].append(line)

    # Apply limit globally (newest first across all years).
    if isinstance(limit, int) and limit >= 0:
        total = 0
        for year in sorted(kept_per_year, reverse=True):
            bullets = kept_per_year[year]
            allowed = max(0, limit - total)
            kept_per_year[year] = bullets[:allowed]
            total += len(kept_per_year[year])

    lines.extend(preamble)
    for year in sorted(kept_per_year, reverse=True):
        bullets = kept_per_year[year]
        if not bullets:
            continue
        lines.append(f"## {year}")
        lines.append("")
        lines.extend(bullets)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- Public entry point ----------------------------------------------------


def query_digest(path: str, kind: str, **filters: Any) -> str:
    """Read a digest file from `path` and return a filtered markdown view.

    If no filters are supplied the file is returned verbatim — same
    behaviour as the previous read_digest implementation.
    """
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    # Strip filter args that weren't actually specified.
    active = {k: v for k, v in filters.items() if v not in (None, "", -1)}
    if not active:
        return text

    if kind == "timeline":
        return _filter_timeline(text, active)
    if kind == "index":
        # Index has no per-row filters; just return as-is.
        return text

    # Table-based digests: parse, filter, re-render. Preserve any
    # non-table prose (e.g. the title / preamble paragraph at the top
    # of the digest) by emitting it ahead of the filtered tables.
    sections = parse_md_tables(text)
    out_parts: List[str] = []
    preamble = _extract_preamble(text)
    if preamble:
        out_parts.append(preamble.rstrip() + "\n")
    for section in sections:
        filtered = filter_rows(section, kind, active)
        if not filtered.rows and section.rows:
            # Filter eliminated the table entirely; drop the heading
            # so the agent isn't looking at empty headers.
            continue
        out_parts.append(render_section(filtered))
    # Filters were active (the no-filter case returned `text` above), so a
    # falsy `out_parts` means the filter legitimately matched nothing — return
    # the empty result, never the full unfiltered digest.
    return "\n".join(out_parts)


def _extract_preamble(text: str) -> str:
    """Return the leading prose (title + description) before the first
    table or sub-heading.

    Stops at whichever comes first: a level-2+ heading (`## …`) or a
    table row (`| … |`). The table boundary matters for digests whose
    single table sits directly under the `# ` title with no intervening
    `## ` heading (threads, people): without it the whole *unfiltered*
    table is swallowed into the preamble and then re-emitted alongside
    the filtered copy — duplicating the output and burying the `limit` /
    `since` filtering that was applied only to the second copy.
    """
    out: List[str] = []
    for line in text.splitlines():
        head = _HEADING_RE.match(line)
        if (head and len(head.group(1)) >= 2) or line.lstrip().startswith("|"):
            break
        out.append(line)
    return "\n".join(out)
