"""Tests for per-WG cache freshness tracking.

Three responsibilities, exercised here:
- record_gather writes the sentinel atomically with a UTC timestamp
- last_gathered round-trips that timestamp
- staleness_warning fires only past the threshold, and is silent
  when the sentinel is missing (legacy caches)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ietf_llm import freshness
from ietf_llm.freshness import (
    _sentinel_path,
    freshness_line,
    last_gathered,
    record_gather,
    staleness_warning,
)


def _backdate(wg: str, days: int) -> None:
    """Rewrite the sentinel to look `days` old."""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    Path(_sentinel_path(wg)).write_text(
        when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        encoding="utf-8",
    )


# --- record_gather / last_gathered round-trip -----------------------------


def test_record_gather_round_trips(isolated_home: Path) -> None:
    record_gather("wg")
    when = last_gathered("wg")
    assert when is not None
    # Within a few seconds of "now" — tolerate slow test machines.
    delta = abs((datetime.now(timezone.utc) - when).total_seconds())
    assert delta < 60
    # Sentinel landed where we said it would, with the expected name.
    sentinel = Path(_sentinel_path("wg"))
    assert sentinel.is_file()
    assert sentinel.read_text().endswith("Z")


def test_record_gather_creates_missing_wg_dir(isolated_home: Path) -> None:
    # No prior cache for this WG — record_gather must mkdir.
    assert not Path(_sentinel_path("brand-new")).parent.exists()
    record_gather("brand-new")
    assert Path(_sentinel_path("brand-new")).is_file()


def test_last_gathered_returns_none_when_missing(isolated_home: Path) -> None:
    assert last_gathered("never-gathered") is None


def test_last_gathered_returns_none_for_malformed(isolated_home: Path) -> None:
    path = Path(_sentinel_path("wg"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a date")
    assert last_gathered("wg") is None


def test_last_gathered_assumes_utc_for_naive_timestamps(
    isolated_home: Path,
) -> None:
    # Older sentinels might lack the Z suffix; treat as UTC rather
    # than refusing them outright.
    path = Path(_sentinel_path("wg"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("2026-01-01T10:00:00")
    when = last_gathered("wg")
    assert when is not None
    assert when.tzinfo is not None


# --- staleness_warning policy ---------------------------------------------


def test_no_warning_when_sentinel_missing(isolated_home: Path) -> None:
    # The deliberate choice: don't nag about caches predating this
    # feature. One gather and we're tracking from then on.
    assert staleness_warning("legacy-wg") is None


def test_no_warning_when_fresh(isolated_home: Path) -> None:
    record_gather("wg")
    assert staleness_warning("wg") is None


def test_no_warning_just_under_threshold(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate("wg", days=freshness.STALE_AFTER_DAYS - 1)
    assert staleness_warning("wg") is None


def test_warning_fires_at_or_past_threshold(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate("wg", days=freshness.STALE_AFTER_DAYS + 1)
    warning = staleness_warning("wg")
    assert warning is not None
    # The warning names the WG, the age in days, and how to refresh.
    assert "wg" in warning
    assert "days ago" in warning
    assert "ietf-llm wg" in warning


def test_custom_threshold_overrides_default(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate("wg", days=2)
    # Tighter threshold: 2-day cache is now stale.
    assert staleness_warning("wg", threshold_days=1) is not None
    # Default threshold: still fresh.
    assert staleness_warning("wg") is None


# --- freshness_line: always reports, escalates when stale ------------------


def test_freshness_line_silent_when_sentinel_missing(isolated_home: Path) -> None:
    assert freshness_line("legacy-wg") is None


def test_freshness_line_reports_date_when_fresh(isolated_home: Path) -> None:
    record_gather("wg")
    line = freshness_line("wg")
    assert line is not None
    # Neutral, informational — the date with no refresh nag.
    assert "gathered" in line.lower()
    assert "today" in line
    assert "⚠" not in line
    assert "refresh" not in line


def test_freshness_line_humanizes_age(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate("wg", days=3)
    line = freshness_line("wg")
    assert line is not None
    assert "3 days ago" in line
    assert "⚠" not in line


def test_freshness_line_escalates_to_warning_when_stale(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate("wg", days=freshness.STALE_AFTER_DAYS + 1)
    line = freshness_line("wg")
    assert line is not None
    # Same escalated text as staleness_warning.
    assert line == staleness_warning("wg")
    assert "⚠" in line
    assert "ietf-llm wg" in line
