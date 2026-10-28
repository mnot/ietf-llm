"""Pieces shared by the two grep backends (`grep` and `grep_rfc`): the
render caps and the helpers both use to bound and describe a hit."""

from __future__ import annotations

import re
from typing import Optional

#: Upper bound on how many matching lines one call will render, so a
#: three-letter pattern can't return the whole corpus. The scan itself is
#: always complete — only the *rendering* is capped, and the cap is reported
#: alongside the true total, so the count stays trustworthy.
_MAX_GREP_LIMIT = 200

#: Longest rendered line before it is elided. Draft text wraps at ~72 columns,
#: but a pasted log line or a base64 blob in a message can run for thousands.
_MAX_LINE_CHARS = 400


def _elide(line: str, matcher: Optional["re.Pattern[str]"] = None) -> str:
    """Bound one rendered line, keeping the match visible.

    Truncating from the left would render a "hit" line with no match in it
    whenever the match sits past the budget — which is precisely the case this
    cap exists for (a pasted log line, a base64 blob). So when the line is over
    budget we window around the first match instead, marking each elided side.
    """
    stripped = line.rstrip()
    if len(stripped) <= _MAX_LINE_CHARS:
        return stripped
    match = matcher.search(stripped) if matcher is not None else None
    if match is None or match.start() < _MAX_LINE_CHARS - 1:
        return stripped[: _MAX_LINE_CHARS - 1] + "…"
    # Centre the window on the match, clamped to the end of the line.
    half = (_MAX_LINE_CHARS - 2) // 2
    start = max(0, match.start() - half)
    end = min(len(stripped), start + _MAX_LINE_CHARS - 2)
    return "…" + stripped[start:end] + ("…" if end < len(stripped) else "")


def _mode_phrase(regex: bool, case_sensitive: bool) -> str:
    return (
        f"{'regex' if regex else 'literal'}, "
        f"{'case-sensitive' if case_sensitive else 'case-insensitive'}"
    )
