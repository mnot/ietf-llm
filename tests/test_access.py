"""Tests for read-path access stamping (coarsening + opt-out).

The durable write is the corpus store's `record_access`, tested elsewhere; here
we exercise `note_access`'s in-process debounce and the env opt-out, with the
store stubbed so we count calls rather than touch a backend.
"""

from __future__ import annotations

from typing import List

import pytest

from ietf_llm import access


@pytest.fixture(autouse=True)
def _reset() -> None:
    access._reset_for_test()


def _stub_store(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Replace the corpus store with one that just records record_access calls;
    return the list the calls land in."""
    calls: List[str] = []

    class _Store:
        def record_access(self, corpus: str) -> None:
            calls.append(corpus)

    monkeypatch.setattr(access, "get_corpus_store", lambda: _Store())
    return calls


def test_note_access_records_once_then_debounces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_store(monkeypatch)
    access.note_access("tls")
    access.note_access("tls")  # within the window -> suppressed
    assert calls == ["tls"]


def test_note_access_stamps_again_after_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_store(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    access.note_access("tls")
    clock[0] += access.STAMP_MIN_INTERVAL_SECONDS + 1
    access.note_access("tls")
    assert calls == ["tls", "tls"]


def test_note_access_is_per_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_store(monkeypatch)
    access.note_access("tls")
    access.note_access("httpbis")
    assert sorted(calls) == ["httpbis", "tls"]


def test_opt_out_skips_the_store_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_store(monkeypatch)
    monkeypatch.setenv("IETF_LLM_RECORD_ACCESS", "off")
    access.note_access("tls")
    assert calls == []
    assert access.record_access_enabled() is False


def test_store_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def record_access(self, corpus: str) -> None:
            raise RuntimeError("read-only IAM role")

    monkeypatch.setattr(access, "get_corpus_store", lambda: _Boom())
    # Must not raise — a failed stamp can never fail the read that triggered it.
    access.note_access("tls")
