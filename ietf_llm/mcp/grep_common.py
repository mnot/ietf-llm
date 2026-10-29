"""Pieces shared by the two grep backends (`grep` and `grep_rfc`): the
render caps and the helpers both use to bound and describe a hit."""

from __future__ import annotations

#: Upper bound on how many matching lines one call will render, so a
#: three-letter pattern can't return the whole corpus. The scan itself is
#: always complete — only the *rendering* is capped, and the cap is reported
#: alongside the true total, so the count stays trustworthy.
_MAX_GREP_LIMIT = 200

#: Scan budget (seconds) for a user-supplied regex over a gathered corpus. The
#: RFC series has its own, `grep_rfc._RFC_SCAN_BUDGET_S`, because ~400 MB is a
#: different scale. Past it the scan is stopped and says so rather than
#: rendering a partial scan as a result.
_REGEX_SCAN_BUDGET_S = 20.0


def _incomplete_scan_head(
    pattern: str, mode: str, budget_s: float, found_note: str, read_note: str
) -> str:
    """The opening of an INCOMPLETE reply, shared by both backends. A scan cut
    short has no denominator, so it must refuse to support a negative claim."""
    return (
        f"**INCOMPLETE SCAN — stopped after {budget_s:.0f}s.** "
        f"`{pattern}` ({mode}): {found_note}, having read {read_note}. "
        "A regex can run for minutes over a large corpus, and a pathological "
        "one never finishes; a literal phrase scans the whole corpus in "
        "seconds. **This cannot support a negative claim** — narrow with "
        "`file_pattern`, or search a literal phrase instead."
    )


def _mode_phrase(regex: bool, case_sensitive: bool) -> str:
    return (
        f"{'regex' if regex else 'literal'}, "
        f"{'case-sensitive' if case_sensitive else 'case-insensitive'}"
    )
