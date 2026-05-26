"""Small pure helpers shared across digest builders.

These are the leaf-level functions: subject normalisation for thread
grouping, date parsing with timezone normalisation, address formatting
for display, GitHub-state case-folding, and file-size formatting for
the index.
"""

from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone
from typing import Any, Optional

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd|fw|aw|sv)\s*:\s*|\[[^\]]+\]\s*)+",
    re.IGNORECASE,
)


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/[wg]-style prefixes for thread grouping."""
    prev = None
    cur = subject.strip()
    # Repeatedly strip until stable (handles "Re: [wg] Re: ...")
    while prev != cur:
        prev = cur
        cur = _SUBJECT_PREFIX_RE.sub("", cur).strip()
    return cur or subject.strip()


def _parse_date(date_header: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 5322 Date header; always return tz-aware (or None).

    Mail dates from the wild come in both tz-aware and tz-naive forms;
    normalising to UTC-aware here means downstream comparisons across
    threads never raise.
    """
    if not date_header:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(date_header))
    except (ValueError, TypeError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _short_addr(from_header: str) -> str:
    """Reduce a From header to a display name or local-part."""
    name, addr = email.utils.parseaddr(from_header)
    if name:
        return name.strip().strip('"')
    if addr and "@" in addr:
        return addr.split("@", 1)[0]
    return from_header.strip() or "(unknown)"


def _state_is_open(state: Any) -> bool:
    """True iff an issue state value represents 'open'.

    GitHub's archive JSON uses both lowercase ('open'/'closed', the
    REST API convention) and uppercase ('OPEN'/'CLOSED', the GraphQL
    convention) depending on which exporter produced the archive.
    Normalise here so downstream sorting and counting work either way.
    """
    return isinstance(state, str) and state.strip().lower() == "open"


def _fmt_size(num: int) -> str:
    """Render a byte count as a short human-readable string (e.g. '1.2MB')."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
