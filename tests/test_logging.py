"""R19: structured (JSON) log records via IETF_LLM_LOG_FORMAT=json for the
container deployment. Text format stays the default for the CLI, and the
visibility rules (ERROR always, STATUS unless quiet, PROGRESS only under
verbose) are unchanged across both formats.
"""

from __future__ import annotations

import json

from ietf_llm import log as logmod
from ietf_llm.log import LogLevel, Verbosity, log


def test_text_is_default(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("plain message", level=LogLevel.STATUS)
    assert capsys.readouterr().err.strip() == "plain message"


def test_text_error_prefix(monkeypatch, capsys):
    # capsys' stderr is not a tty, so no colour is applied.
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("boom", level=LogLevel.ERROR)
    assert capsys.readouterr().err.strip() == "[ERROR] boom"


def test_text_warn_prefix(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("careful", level=LogLevel.WARN)
    assert capsys.readouterr().err.strip() == "[WARN] careful"


def test_warn_shown_at_status_verbosity(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("careful", verbosity=Verbosity.STATUS, level=LogLevel.WARN)
    assert capsys.readouterr().err.strip() == "[WARN] careful"


def test_warn_hidden_when_quiet(monkeypatch, capsys):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    log("careful", verbosity=Verbosity.QUIET, level=LogLevel.WARN)
    assert capsys.readouterr().err == ""


def test_color_wraps_level_tag_on_tty(monkeypatch, capsys):
    # When colour is enabled, only the bracketed tag is wrapped — not the
    # message — and a reset follows before the space.
    monkeypatch.setattr(logmod, "_use_color", lambda: True)
    log("boom", level=LogLevel.ERROR)
    log("careful", level=LogLevel.WARN)
    err = capsys.readouterr().err
    assert "\033[31m[ERROR]\033[0m boom" in err
    assert "\033[33m[WARN]\033[0m careful" in err


def test_no_color_when_uncoloured_level(monkeypatch, capsys):
    # STATUS has no tag, so colour gating never touches it.
    monkeypatch.setattr(logmod, "_use_color", lambda: True)
    log("plain", level=LogLevel.STATUS)
    assert capsys.readouterr().err.strip() == "plain"


def test_use_color_gating(monkeypatch):
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _Fake:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr(logmod.sys, "stderr", _Fake(True))
    assert logmod._use_color() is True
    monkeypatch.setattr(logmod.sys, "stderr", _Fake(False))
    assert logmod._use_color() is False
    # NO_COLOR (any value, even empty) disables it even on a tty.
    monkeypatch.setattr(logmod.sys, "stderr", _Fake(True))
    monkeypatch.setenv("NO_COLOR", "")
    assert logmod._use_color() is False
    monkeypatch.delenv("NO_COLOR", raising=False)
    # JSON mode never colours.
    monkeypatch.setenv("IETF_LLM_LOG_FORMAT", "json")
    assert logmod._use_color() is False


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
