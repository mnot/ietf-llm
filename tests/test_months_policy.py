"""Tests for the months-window policy (ietf_llm.months.months_request_error /
months_request_caution): months=0 is the all-history sentinel and is refused
without force, negatives are refused, and a large bounded window draws a
non-blocking caution."""

from __future__ import annotations

from ietf_llm.months import DEFAULT_MONTHS, months_request_caution, months_request_error


def test_unset_months_is_allowed() -> None:
    assert months_request_error(None, force=False) is None
    assert months_request_caution(None) is None


def test_bounded_window_is_allowed() -> None:
    assert months_request_error(6, force=False) is None
    assert months_request_error(DEFAULT_MONTHS, force=False) is None


def test_zero_months_refused_without_force() -> None:
    err = months_request_error(0, force=False)
    assert err is not None and "entire list history" in err


def test_zero_months_allowed_with_force() -> None:
    assert months_request_error(0, force=True) is None


def test_negative_months_refused_even_with_force() -> None:
    assert months_request_error(-3, force=True) is not None


def test_default_window_has_no_caution() -> None:
    assert months_request_caution(DEFAULT_MONTHS) is None


def test_large_bounded_window_cautions() -> None:
    note = months_request_caution(DEFAULT_MONTHS + 24)
    assert note is not None and "longer gather" in note
