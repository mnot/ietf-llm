"""Tests for the small pure helpers in ietf_llm.digest.

These cover the three specific bug classes that bit us this session:
  - _normalize_subject must collapse repeated Re:/Fwd:/[wg] prefixes
  - _state_is_open must case-fold ("OPEN" and "open" both → True)
  - _parse_date must always return tz-aware (or None), so downstream
    datetime comparisons across messages don't raise.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ietf_llm.digest import (
    _normalize_subject,
    _parse_date,
    _short_addr,
    _state_is_open,
)


# --- _normalize_subject ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Cookie partitioning", "Cookie partitioning"),
        ("Re: Cookie partitioning", "Cookie partitioning"),
        ("[wg] Cookie partitioning", "Cookie partitioning"),
        ("Re: [wg] Cookie partitioning", "Cookie partitioning"),
        ("[wg] Re: Cookie partitioning", "Cookie partitioning"),
        ("Re: Re: Re: [wg] Cookie partitioning", "Cookie partitioning"),
        ("Fwd: Cookie partitioning", "Cookie partitioning"),
        ("FW: Cookie partitioning", "Cookie partitioning"),
        ("AW: Cookie partitioning", "Cookie partitioning"),  # German "Re:"
        ("SV: Cookie partitioning", "Cookie partitioning"),  # Swedish "Re:"
        ("  Re:   Cookie partitioning  ", "Cookie partitioning"),
        # Real-world: nested list-tag and Re: chains, plus a colon in body
        ("Re: [ai-control] Re: WGLC: Foo", "WGLC: Foo"),
    ],
)
def test_normalize_subject(raw: str, expected: str) -> None:
    assert _normalize_subject(raw) == expected


def test_normalize_subject_empty_falls_back_to_raw_stripped() -> None:
    # Stripping all prefixes from "Re:" alone leaves nothing; the function
    # should fall back to the raw subject (stripped) rather than empty.
    assert _normalize_subject("Re:") == "Re:"


# --- _state_is_open --------------------------------------------------------
# This is the regression I shipped: github.com archive JSON uses both
# 'open' (REST) and 'OPEN' (GraphQL) and the digest code only matched
# lowercase — so an uppercase archive bucketed every open issue as closed.


@pytest.mark.parametrize(
    "state,expected",
    [
        ("open", True),
        ("OPEN", True),
        ("Open", True),
        ("  open  ", True),  # tolerated leading/trailing whitespace
        ("closed", False),
        ("CLOSED", False),
        ("", False),
        ("anything-else", False),
    ],
)
def test_state_is_open(state: str, expected: bool) -> None:
    assert _state_is_open(state) is expected


@pytest.mark.parametrize("state", [None, 42, [], {}])
def test_state_is_open_handles_non_string(state: object) -> None:
    assert _state_is_open(state) is False


# --- _parse_date -----------------------------------------------------------
# This is the second regression: IETF mail headers mix tz-aware
# ("Mon, 01 Jan 2025 10:00:00 +0000") and tz-naive ("Mon, 01 Jan 2025
# 10:00:00") forms, and comparing them raises TypeError. Normalising to
# UTC-aware at parse time avoids that everywhere downstream.


def test_parse_date_none_returns_none() -> None:
    assert _parse_date(None) is None


def test_parse_date_malformed_returns_none() -> None:
    assert _parse_date("not a date") is None


def test_parse_date_aware_input_stays_aware() -> None:
    dt = _parse_date("Mon, 01 Jan 2025 10:00:00 +0000")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_date_naive_input_becomes_utc_aware() -> None:
    # No timezone in the header — should be normalised to UTC.
    dt = _parse_date("Mon, 01 Jan 2025 10:00:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_parse_date_mixed_aware_naive_compare_does_not_raise() -> None:
    # The whole point of normalising: thread building compares dates
    # across many messages from many clients. Mixed forms must not raise.
    a = _parse_date("Mon, 01 Jan 2025 10:00:00 +0000")
    b = _parse_date("Tue, 02 Jan 2025 10:00:00")  # naive
    assert a is not None and b is not None
    assert a < b  # would raise TypeError pre-fix


def test_parse_date_can_compare_with_datetime_min_utc() -> None:
    # The sort key in _build_threads_digest uses datetime.min.replace(
    # tzinfo=timezone.utc) as a fallback. Real parsed dates must
    # compare against it without raising.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    dt = _parse_date("Mon, 01 Jan 2025 10:00:00 +0000")
    assert dt is not None
    assert dt > epoch


# --- _short_addr -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alice <a@x>", "Alice"),
        ('"Alice" <a@x>', "Alice"),
        ("a@x", "a"),
        ("<a@x>", "a"),
        ("", "(unknown)"),
    ],
)
def test_short_addr(raw: str, expected: str) -> None:
    assert _short_addr(raw) == expected
