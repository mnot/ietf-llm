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

import pytest

from ietf_llm import freshness
from ietf_llm.freshness import (
    _sentinel_path,
    debounce_reason,
    freshness_line,
    gather_min_interval_hours,
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


def _backdate_hours(wg: str, hours: float) -> None:
    """Rewrite the sentinel to look `hours` old."""
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
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


# --- gather_min_interval_hours: env resolution ----------------------------


def test_min_interval_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(freshness._MIN_INTERVAL_ENV, raising=False)
    assert gather_min_interval_hours() == freshness.GATHER_MIN_INTERVAL_DEFAULT_HOURS


def test_min_interval_reads_float_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, "0.5")
    assert gather_min_interval_hours() == 0.5


def test_min_interval_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, "0")
    assert gather_min_interval_hours() == 0.0


@pytest.mark.parametrize("value", ["nonsense", "-3", "  "])
def test_min_interval_falls_back_to_default_on_bad_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Malformed or negative -> default, never an exception that aborts a gather.
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, value)
    assert gather_min_interval_hours() == freshness.GATHER_MIN_INTERVAL_DEFAULT_HOURS


# --- debounce_reason: the gather-entry freshness gate ----------------------


def test_debounce_none_when_never_gathered(isolated_home: Path) -> None:
    # No sentinel: a first gather is never debounced.
    assert debounce_reason("never") is None


def test_debounce_fires_within_window(isolated_home: Path) -> None:
    record_gather("wg")  # just now, default 6h window
    reason = debounce_reason("wg")
    assert reason is not None
    # Names the corpus, the age, the window, and that it was skipped.
    assert "wg" in reason
    assert "skipped" in reason
    assert "freshness window" in reason


def test_debounce_none_once_past_window(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate_hours("wg", hours=freshness.GATHER_MIN_INTERVAL_DEFAULT_HOURS + 1)
    assert debounce_reason("wg") is None


def test_debounce_disabled_when_window_zero(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_gather("wg")
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, "0")
    assert debounce_reason("wg") is None


def test_debounce_honours_env_window(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_gather("wg")
    _backdate_hours("wg", hours=2)
    # 1h window: a 2h-old cache is now past it.
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, "1")
    assert debounce_reason("wg") is None
    # 6h window: still fresh.
    monkeypatch.setenv(freshness._MIN_INTERVAL_ENV, "6")
    assert debounce_reason("wg") is not None


def test_debounce_explicit_interval_overrides_env(isolated_home: Path) -> None:
    record_gather("wg")
    _backdate_hours("wg", hours=3)
    assert debounce_reason("wg", min_interval_hours=2) is None
    assert debounce_reason("wg", min_interval_hours=4) is not None
