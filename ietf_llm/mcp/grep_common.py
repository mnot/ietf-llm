"""Pieces shared by the two grep backends (`grep` and `grep_rfc`): the
render caps and the helpers both use to bound and describe a hit."""

from __future__ import annotations

from typing import Optional

from .. import serve_metrics
from ..log import LogLevel, Verbosity, log
from ..regexscan import ScanFailed

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


#: What a client is told when the scan process failed. The child's stderr
#: (paths, tracebacks) stays in the server log.
_SCAN_FAILED_REPLY = (
    "The regex scan failed in its worker process. **Nothing can be concluded "
    "from this.** Retry, or narrow with `file_pattern`."
)


def _note_scan_stop(reason: str, failure: Optional[ScanFailed] = None) -> None:
    """Log and count a regex scan that was killed (`timeout`) or failed
    (`failed`): the reply is an ordinary string, so RED metrics would count it
    a success and nothing else would show it."""
    serve_metrics.record_scan_stop(reason)
    detail = f": {failure.detail}" if failure is not None and failure.detail else ""
    log(
        f"grep_corpus regex scan {reason}{detail}",
        Verbosity.STATUS,
        level=LogLevel.WARN,
        fields={"event": "grep_scan_stop", "reason": reason},
    )
