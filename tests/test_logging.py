"""R19: structured (JSON) log records via IETF_LLM_LOG_FORMAT=json for the
container deployment. Text format stays the default for the CLI, and the
visibility rules (ERROR always, STATUS unless quiet, PROGRESS only under
verbose) are unchanged across both formats.
"""

from __future__ import annotations

import json

from ietf_llm.utils import LogLevel, Verbosity, log


def test_text_is_default(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("plain message", level=LogLevel.STATUS)
    assert capsys.readouterr().err.strip() == "plain message"


def test_text_error_prefix(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("boom", level=LogLevel.ERROR)
    assert capsys.readouterr().err.strip() == "[ERROR] boom"


def test_json_format(monkeypatch, capsys):
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("hello", level=LogLevel.STATUS)
    rec = json.loads(capsys.readouterr().err.strip())
    assert rec["msg"] == "hello"
    assert rec["level"] == "status"
    assert rec["ts"].endswith("Z")


def test_json_error_level(monkeypatch, capsys):
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("oops", level=LogLevel.ERROR)
    rec = json.loads(capsys.readouterr().err.strip())
    assert rec["level"] == "error"
    assert rec["msg"] == "oops"


def test_quiet_suppresses_in_json(monkeypatch, capsys):
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("hidden", verbosity=Verbosity.QUIET, level=LogLevel.PROGRESS)
    assert capsys.readouterr().err == ""


def test_progress_hidden_at_status_verbosity(monkeypatch, capsys):
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("detail", verbosity=Verbosity.STATUS, level=LogLevel.PROGRESS)
    assert capsys.readouterr().err == ""


def test_json_fields_merged(monkeypatch, capsys):
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("tool overview ok 12ms", level=LogLevel.STATUS, fields={"tool": "overview"})
    rec = json.loads(capsys.readouterr().err.strip())
    assert rec["tool"] == "overview"
    assert rec["msg"] == "tool overview ok 12ms"
    assert rec["level"] == "status"


def test_json_fixed_keys_win_over_fields(monkeypatch, capsys):
    # A field can't clobber the fixed ts/level/msg shape every record carries.
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    log("real", level=LogLevel.STATUS, fields={"msg": "spoof", "level": "error"})
    rec = json.loads(capsys.readouterr().err.strip())
    assert rec["msg"] == "real"
    assert rec["level"] == "status"


def test_text_mode_ignores_fields(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("summary", level=LogLevel.STATUS, fields={"tool": "overview"})
    assert capsys.readouterr().err.strip() == "summary"
