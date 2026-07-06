"""Tests for gather-side HTTP egress metrics (issue #44).

Covers the accumulator's classification / byte / host accounting, the
URL-pattern folding used to spot hot loops, persistence, thread-local
isolation, and that the two instrumented chokepoints (`fetch_resource`
and `_get_json`) actually record into the current accumulator.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import requests

from ietf_llm import http_metrics, net
from ietf_llm.gather.sources import datatracker

# Captured at import (before conftest's autouse `_no_datatracker` stub binds
# over it) so the chokepoint test can restore the genuine `_get_json`.
_REAL_GET_JSON = datatracker._get_json


# --- pattern folding -------------------------------------------------------


def test_url_pattern_collapses_ids_and_sorts_query_keys() -> None:
    a = "https://datatracker.ietf.org/api/v1/doc/document/?group__acronym=tls&type=draft&limit=200"
    b = "https://datatracker.ietf.org/api/v1/doc/document/?type=rfc&group__acronym=quic&limit=200"
    # Same endpoint shape, different group / page → one pattern.
    assert http_metrics.url_pattern(a) == http_metrics.url_pattern(b)
    assert http_metrics.url_pattern(a) == (
        "datatracker.ietf.org/api/v#/doc/document/?group__acronym&limit&type"
    )
    # Every run of digits in the path folds to `#` (the API version too).
    assert http_metrics.url_pattern(
        "https://datatracker.ietf.org/api/v1/person/email/12345/"
    ) == "datatracker.ietf.org/api/v#/person/email/#/"


# --- classification / accounting -------------------------------------------


def test_record_classifies_and_accumulates() -> None:
    m = http_metrics.HttpMetrics()
    m.record("https://datatracker.ietf.org/x", 200, 100)
    m.record("https://datatracker.ietf.org/y", 304, 0)
    m.record("https://github.com/z", 500, 50, error=True)
    assert (m.ok, m.revalidated, m.errors, m.total) == (1, 1, 1, 3)
    assert m.bytes_received == 150
    assert m.host_requests["datatracker.ietf.org"] == 2
    assert m.host_requests["github.com"] == 1
    assert m.host_bytes["datatracker.ietf.org"] == 100


def test_negative_or_none_bytes_clamped() -> None:
    m = http_metrics.HttpMetrics()
    m.record("https://h/x", 200, -5)
    m.record("https://h/y", 200, None)  # type: ignore[arg-type]
    assert m.bytes_received == 0


def test_summary_line_empty_and_populated() -> None:
    assert http_metrics.HttpMetrics().summary_line() == "Network: no upstream requests."
    m = http_metrics.HttpMetrics()
    m.record("https://datatracker.ietf.org/x", 200, 4096)
    m.record("https://datatracker.ietf.org/y", 304, 0)
    line = m.summary_line()
    assert "2 requests" in line
    assert "1 transferred, 1 revalidated, 0 errors" in line
    assert "4.1 KB" in line
    assert "datatracker.ietf.org 2" in line


def test_to_dict_shape_and_top_patterns() -> None:
    m = http_metrics.HttpMetrics()
    for _ in range(3):
        m.record("https://datatracker.ietf.org/api/v1/doc/document/?group__acronym=tls", 200, 10)
    m.record("https://github.com/o/r", 200, 5)
    d = m.to_dict()
    assert d["requests"] == 4 and d["ok"] == 4 and d["bytes_received"] == 35
    assert d["by_host"]["datatracker.ietf.org"] == {"requests": 3, "bytes": 30}
    # Hottest pattern first.
    assert d["top_patterns"][0]["requests"] == 3
    assert "doc/document" in d["top_patterns"][0]["pattern"]


def test_humanize_bytes() -> None:
    assert http_metrics._humanize_bytes(512) == "512 B"
    assert http_metrics._humanize_bytes(4096) == "4.1 KB"
    assert http_metrics._humanize_bytes(1_800_000) == "1.8 MB"


# --- thread-local current accumulator --------------------------------------


def test_reset_and_current_are_thread_local() -> None:
    main = http_metrics.reset()
    main.record("https://h/main", 200, 1)

    other: Dict[str, Any] = {}

    def worker() -> None:
        # A fresh thread starts with its own empty accumulator.
        other["before"] = http_metrics.current().total
        http_metrics.record("https://h/worker", 200, 1)
        other["after"] = http_metrics.current().total

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other == {"before": 0, "after": 1}
    # The worker's request did not leak into this thread's accumulator.
    assert http_metrics.current().total == 1


def test_persist_writes_json(tmp_path: Path) -> None:
    http_metrics.reset()
    http_metrics.record("https://datatracker.ietf.org/x", 200, 7)
    http_metrics.persist(str(tmp_path))
    data = json.loads((tmp_path / "gather-metrics.json").read_text())
    assert data["requests"] == 1 and data["bytes_received"] == 7
    assert data["by_host"]["datatracker.ietf.org"]["bytes"] == 7


def test_persist_is_best_effort(tmp_path: Path) -> None:
    # A path whose parent is a file, not a dir → makedirs fails; no raise.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    http_metrics.reset()
    http_metrics.persist(str(not_a_dir / "sub"))  # must not raise


# --- chokepoint integration ------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body: bytes, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers: Dict[str, str] = {}
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err

    def json(self) -> Any:
        return self._payload


def test_fetch_resource_records(monkeypatch: pytest.MonkeyPatch) -> None:
    http_metrics.reset()
    monkeypatch.setattr(
        net.http_session(), "get", lambda *a, **k: _FakeResp(200, b"hello")
    )
    assert net.fetch_resource("https://datatracker.ietf.org/r") is not None
    m = http_metrics.current()
    assert (m.ok, m.errors, m.bytes_received) == (1, 0, 5)


def test_fetch_resource_records_error(monkeypatch: pytest.MonkeyPatch) -> None:
    http_metrics.reset()
    monkeypatch.setattr(
        net.http_session(), "get", lambda *a, **k: _FakeResp(404, b"nope")
    )
    assert net.fetch_resource("https://datatracker.ietf.org/missing") is None
    m = http_metrics.current()
    assert (m.ok, m.errors, m.bytes_received) == (0, 1, 4)


def test_get_json_records_200_and_304(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Hermetic cache dir so the shared on-disk ETag store is untouched.
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(tmp_path))
    # Force the lazy process-default ETag store to rebuild from the empty dir.
    monkeypatch.setattr(datatracker, "_DEFAULT_CACHE", None)
    # Undo conftest's blanket `_get_json` stub for this test.
    monkeypatch.setattr(datatracker, "_get_json", _REAL_GET_JSON)

    http_metrics.reset()
    monkeypatch.setattr(
        net.http_session(),
        "get",
        lambda *a, **k: _FakeResp(200, b'{"a": 1}', {"a": 1}),
    )
    assert datatracker._get_json("/api/v1/x") == {"a": 1}

    monkeypatch.setattr(
        net.http_session(), "get", lambda *a, **k: _FakeResp(304, b"")
    )
    datatracker._get_json("/api/v1/y")
    m = http_metrics.current()
    assert (m.ok, m.revalidated, m.errors) == (1, 1, 0)
