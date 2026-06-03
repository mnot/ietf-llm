"""Tests for the MCP gather tools and their opt-in gate
(ietf_llm.mcp_server.tool_start_gather / tool_gather_status /
_gather_enabled / status formatting). The runner itself is stubbed.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ietf_llm import gather_runner, mcp_server


# --- _gather_enabled ------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_gather_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", value)
    assert mcp_server._gather_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_gather_enabled_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", value)
    assert mcp_server._gather_enabled() is False


def test_gather_enabled_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    assert mcp_server._gather_enabled() is False


# --- tool_start_gather ----------------------------------------------------


def test_start_gather_forwards_spec_and_reports_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    def fake_start(spec: gather_runner.GatherSpec) -> Dict[str, Any]:
        captured["spec"] = spec
        return {"started": True, "corpus": spec.corpus}

    monkeypatch.setattr(gather_runner, "start", fake_start)
    out = mcp_server.tool_start_gather(
        "tls", mailing_list=["tls@ietf.org"], github=["o/r"], months=6
    )
    assert "Started gathering 'tls'" in out
    assert "gather_status" in out
    spec = captured["spec"]
    assert spec.corpus == "tls"
    assert spec.mailing_list == ["tls@ietf.org"]
    assert spec.github == ["o/r"]
    assert spec.months == 6


def test_start_gather_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {"started": False, "reason": "already running"},
    )
    out = mcp_server.tool_start_gather("tls")
    assert "already running" in out


def test_start_gather_rejects_blank_corpus() -> None:
    out = mcp_server.tool_start_gather("   ")
    assert "corpus name" in out


@pytest.mark.parametrize("name", ["../evil", "a/b", "/etc", "-flag", ".hidden"])
def test_start_gather_rejects_unsafe_name_without_calling_runner(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def boom(_spec: Any) -> Any:
        raise AssertionError("runner.start must not be reached for unsafe names")

    monkeypatch.setattr(gather_runner, "start", boom)
    out = mcp_server.tool_start_gather(name)
    assert "not a valid corpus name" in out


# --- tool_gather_status ---------------------------------------------------


def test_gather_status_one_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gather_runner, "read_status",
        lambda corpus: {
            "corpus": corpus, "state": "running", "stage": "github issues",
            "stage_index": 7, "stage_total": 17,
            "started": "2026-06-03T12:00:00Z", "finished": None,
        },
    )
    out = mcp_server.tool_gather_status("tls")
    assert "**tls**" in out and "running" in out
    assert "stage 7/17 (github issues)" in out


def test_gather_status_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_runner, "read_status", lambda corpus: None)
    out = mcp_server.tool_gather_status("ghost")
    assert "No gather has been recorded for 'ghost'" in out


def test_gather_status_all(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: List[Dict[str, Any]] = [
        {"corpus": "tls", "state": "done", "started": "2026-06-03T12:00:00Z",
         "finished": "2026-06-03T12:03:20Z"},
        {"corpus": "quic", "state": "failed", "error": "boom",
         "started": "2026-06-03T12:00:00Z", "finished": "2026-06-03T12:00:05Z"},
    ]
    monkeypatch.setattr(gather_runner, "all_statuses", lambda: rows)
    out = mcp_server.tool_gather_status(None)
    assert "**tls** — done" in out
    assert "**quic** — failed" in out and "error: boom" in out


def test_gather_status_all_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_runner, "all_statuses", lambda: [])
    assert "No gathers" in mcp_server.tool_gather_status(None)


# --- formatting helpers ---------------------------------------------------


def test_format_done_includes_elapsed() -> None:
    out = mcp_server._format_gather_status(
        {"corpus": "tls", "state": "done",
         "started": "2026-06-03T12:00:00Z", "finished": "2026-06-03T12:03:20Z"}
    )
    assert "**tls** — done" in out
    assert "3m20s" in out


def test_format_failed_includes_error() -> None:
    out = mcp_server._format_gather_status(
        {"corpus": "quic", "state": "failed", "error": "network down",
         "started": "2026-06-03T12:00:00Z", "finished": "2026-06-03T12:00:09Z"}
    )
    assert "error: network down" in out
    assert "9s" in out


def test_elapsed_handles_missing_timestamps() -> None:
    assert mcp_server._gather_elapsed({}) == ""
