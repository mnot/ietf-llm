"""Generic text helpers used across digest builders, thread
reconstruction, and chunking.

These are not digest-specific — `_normalize_subject` works for any
mail-derived data, `_parse_date` is just RFC 5322 with tz normalisation,
`_short_addr` is a display helper. Promoting them out of `digest/` lets
non-digest consumers (e.g. `mail_threads`) use them without dragging the
digest package's other dependencies along (and avoids a circular import
when `digest/threads.py` consumes `mail_threads.build_threads()`).
"""

from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone
from typing import Optional

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
