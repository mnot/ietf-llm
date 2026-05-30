"""Per-WG cache freshness tracking.

A single sentinel file `~/.cache/ietf-llm/<wg>/last-gathered` holds an
ISO 8601 UTC timestamp recording the most recent successful gather.
Three subsystems read or write it:

  - `__main__._gather_one` writes it at the end of every gather.
  - `export_cli` warns on stderr if the cache is stale.
  - `mcp_server` prepends a one-line warning to the top-level tool
    responses (overview, read_digest, search_corpus, list_files) so
    a consuming LLM can flag the staleness to the user.

We deliberately don't warn when the sentinel is *missing* (the cache
predates this feature, or the user populated it some other way) —
forcing a re-gather to populate the sentinel would be noisier than
useful. One real gather and we're tracking it from there on.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from .utils import get_cache_dir

#: Cache older than this prompts the warning. Picked to be long enough
#: that day-to-day reading doesn't constantly nag, but short enough that
#: month-old material gets flagged before a user trusts it.
STALE_AFTER_DAYS = 7

_SENTINEL_NAME = "last-gathered"


def _sentinel_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, _SENTINEL_NAME)


def record_gather(wg: str) -> None:
    """Write the current UTC time to the WG's `last-gathered` sentinel.

    Best-effort: if the WG cache directory doesn't exist yet, or the
    write fails, we don't raise. Freshness tracking is a UX hint, not
    a correctness invariant; a failed write just means the next
    consumer falls back to the "unknown / treat as fresh" path.
    """
    path = _sentinel_path(wg)
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_now_iso())
    except OSError:
        pass


def last_gathered(wg: str) -> Optional[datetime]:
    """Read the sentinel and return a tz-aware UTC datetime, or None
    if missing / unreadable / malformed.
    """
    path = _sentinel_path(wg)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        # Python's fromisoformat accepts the trailing-Z form on 3.11+;
        # normalise older variants by rewriting to +00:00.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _humanize_age(age_days: int) -> str:
    """`today` / `1 day ago` / `N days ago`."""
    if age_days <= 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def _stale_warning(wg: str, age_days: int, date: str) -> str:
    """The escalated, stale-cache warning line (with refresh prompt)."""
    return (
        f"⚠ {wg} cache last gathered {age_days} days ago "
        f"({date}); run `ietf-llm {wg}` to refresh."
    )


def staleness_warning(wg: str, threshold_days: int = STALE_AFTER_DAYS) -> Optional[str]:
    """Return a one-line warning if the cache is older than the threshold.

    Returns None when the cache is fresh, or when we have no record of
    when it was last gathered (see module docstring for why we don't
    warn on absence). Single line — callers prepend or print as-is. Used
    by the export CLI, which only wants to speak up when something is
    actually stale.
    """
    when = last_gathered(wg)
    if when is None:
        return None
    age_days = (datetime.now(timezone.utc) - when).days
    if age_days < threshold_days:
        return None
    return _stale_warning(wg, age_days, when.strftime("%Y-%m-%d"))


def freshness_line(wg: str, threshold_days: int = STALE_AFTER_DAYS) -> Optional[str]:
    """A one-line freshness note for *every* top-level tool response.

    Always reports when the corpus was gathered, so a consumer knows the
    floor on their view (a question like "what happened today" is shaped
    by it). Escalates to the `staleness_warning` refresh prompt once the
    cache is older than `threshold_days`. Returns None only when there is
    no record of the gather time (absence is silent — see module docstring).
    """
    when = last_gathered(wg)
    if when is None:
        return None
    age_days = (datetime.now(timezone.utc) - when).days
    date = when.strftime("%Y-%m-%d")
    if age_days >= threshold_days:
        return _stale_warning(wg, age_days, date)
    return f"_{wg} corpus gathered {date} ({_humanize_age(age_days)})._"


def _now_iso() -> str:
    """ISO 8601 UTC string with a trailing Z, matching chunk_date format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
