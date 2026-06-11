"""Tests for the MCP gather tools and their opt-in gate
(ietf_llm.mcp_server.tool_start_gather / tool_gather_status /
_gather_enabled / status formatting). The runner itself is stubbed.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ietf_llm import freshness, gather_runner, mcp_server


# --- _gather_enabled ------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_gather_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", value)
    assert mcp_server._gather_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_gather_enabled_explicit_falsy(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # An explicit falsy value forces gather off regardless of the default.
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", True)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", value)
    assert mcp_server._gather_enabled() is False


def test_gather_enabled_unset_follows_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the env unset, the result is whatever default was last resolved
    # (the MCP server sets it from the transport at startup).
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", True)
    assert mcp_server._gather_enabled() is True
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", False)
    assert mcp_server._gather_enabled() is False


def test_gather_enabled_unrecognised_follows_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A value that is neither truthy nor falsy is treated as unset.
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", True)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "maybe")
    assert mcp_server._gather_enabled() is True


def test_explicit_truthy_overrides_off_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env is an override in both directions: on, even when default is off.
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", False)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    assert mcp_server._gather_enabled() is True


def test_set_gather_default_maps_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    # This is the rule `main` applies: stdio -> on, http -> off.
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    saved = freshness._GATHER_DEFAULT
    try:
        freshness.set_gather_default(True)
        assert freshness.gather_enabled() is True
        freshness.set_gather_default(False)
        assert freshness.gather_enabled() is False
    finally:
        freshness.set_gather_default(saved)


def test_import_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Baseline for the CLI / import contexts that never call set_gather_default:
    # gather stays off until the MCP server resolves a transport.
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    assert freshness._GATHER_DEFAULT is False
    assert mcp_server._gather_enabled() is False


def test_gather_hint_phrasing_follows_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drift guard: the "go gather" hint and the registration gate read the
    # same resolved flag, so the hint names start_gather iff gather is enabled.
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", True)
    assert "start_gather" in freshness.gather_suggestion("tls")
    monkeypatch.setattr(freshness, "_GATHER_DEFAULT", False)
    assert "ietf-llm tls" in freshness.gather_suggestion("tls")


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


def test_start_gather_refuses_zero_months_without_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_start(spec: gather_runner.GatherSpec) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {"started": True, "corpus": spec.corpus}

    monkeypatch.setattr(gather_runner, "start", fake_start)
    out = mcp_server.tool_start_gather("tls", months=0)
    assert "force" in out
    assert called is False  # refused before a slot is spent


def test_start_gather_allows_zero_months_with_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {"started": True, "corpus": spec.corpus},
    )
    out = mcp_server.tool_start_gather("tls", months=0, force=True)
    assert "Started gathering 'tls'" in out


def test_start_gather_cautions_on_large_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {"started": True, "corpus": spec.corpus},
    )
    out = mcp_server.tool_start_gather("tls", months=36)
    assert "Started gathering 'tls'" in out
    assert "36-month window" in out


def test_start_gather_surfaces_stop_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {"started": True, "corpus": spec.corpus, "cancel_token": "tok123"},
    )
    out = mcp_server.tool_start_gather("tls")
    assert "stop_gather" in out and "tok123" in out


# --- tool_stop_gather -----------------------------------------------------


def test_stop_gather_forwards_and_reports_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: Dict[str, Any] = {}

    def fake_stop(corpus: str, token: str) -> Dict[str, Any]:
        captured["args"] = (corpus, token)
        return {"stopped": True, "corpus": corpus}

    monkeypatch.setattr(gather_runner, "request_stop", fake_stop)
    out = mcp_server.tool_stop_gather("tls", "tok123")
    assert captured["args"] == ("tls", "tok123")
    assert "Requested stop for 'tls'" in out and "gather_status" in out


def test_stop_gather_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gather_runner, "request_stop",
        lambda corpus, token: {"stopped": False, "reason": "bad token"},
    )
    out = mcp_server.tool_stop_gather("tls", "wrong")
    assert "does not match" in out


def test_stop_gather_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gather_runner, "request_stop",
        lambda corpus, token: {"stopped": False, "reason": "not running", "state": "done"},
    )
    out = mcp_server.tool_stop_gather("tls", "tok")
    assert "nothing to stop" in out and "done" in out


def test_stop_gather_rejects_unsafe_name(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_stop(corpus: str, token: str) -> Dict[str, Any]:
        nonlocal called
        called = True
        return {"stopped": True}

    monkeypatch.setattr(gather_runner, "request_stop", fake_stop)
    out = mcp_server.tool_stop_gather("../etc", "tok")
    assert "not a valid corpus name" in out
    assert called is False


def test_start_gather_forwards_force(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, Any] = {}

    def fake_start(spec: gather_runner.GatherSpec) -> Dict[str, Any]:
        captured["spec"] = spec
        return {"started": True, "corpus": spec.corpus}

    monkeypatch.setattr(gather_runner, "start", fake_start)
    mcp_server.tool_start_gather("tls", force=True)
    assert captured["spec"].force is True


def test_start_gather_similar_exists_steers_to_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {
            "started": False,
            "reason": "similar exists",
            "detail": "'x-new' overlaps an existing corpus: 'x-old' already "
            "covers draft:draft-foo.",
            "corpus": "x-new",
        },
    )
    out = mcp_server.tool_start_gather("x-new", draft=["draft-foo"])
    # Names the existing corpus, steers to reuse, and offers force as the out.
    assert "x-old" in out
    assert "near-duplicate" in out
    assert "force=True" in out


def test_start_gather_fresh_is_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gather_runner, "start",
        lambda spec: {
            "started": False,
            "reason": "fresh",
            "detail": "tls was gathered 5m ago, within the 6h window — skipped.",
            "corpus": "tls",
        },
    )
    out = mcp_server.tool_start_gather("tls")
    # Surfaces the skip detail, frames it as success, and points at force=True.
    assert "skipped" in out
    assert "success" in out
    assert "force=True" in out
    assert "already running" not in out


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


def test_gather_status_renders_stage_detail() -> None:
    out = mcp_server._format_gather_status(
        {
            "corpus": "httpbis", "state": "running", "stage": "mailing list",
            "stage_index": 3, "stage_total": 17,
            "stage_detail": "ietf-http-wg: 1200/8000 messages downloaded",
            "started": "2026-06-03T12:00:00Z", "finished": None,
        }
    )
    assert "stage 3/17 (mailing list)" in out
    assert "1200/8000 messages downloaded" in out


def test_gather_status_running_notes_stop_requested() -> None:
    out = mcp_server._format_gather_status(
        {
            "corpus": "tls", "state": "running", "stage": "mailing list",
            "stage_index": 3, "stage_total": 17, "cancel_requested": True,
            "started": "2026-06-03T12:00:00Z", "finished": None,
        }
    )
    assert "stop requested" in out


def test_gather_status_renders_cancelled() -> None:
    out = mcp_server._format_gather_status(
        {
            "corpus": "tls", "state": "cancelled",
            "started": "2026-06-03T12:00:00Z", "finished": "2026-06-03T12:01:00Z",
        }
    )
    assert "**tls**" in out and "cancelled" in out
    assert "partial gather discarded" in out


def test_gather_status_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_runner, "read_status", lambda corpus: None)
    out = mcp_server.tool_gather_status("ghost")
    assert "No gather has been recorded for 'ghost'" in out


def test_gather_status_rejects_unsafe_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_corpus: str) -> Any:
        raise AssertionError("read_status must not see an unsafe name")

    monkeypatch.setattr(gather_runner, "read_status", boom)
    out = mcp_server.tool_gather_status("../etc/passwd")
    assert "not a valid corpus name" in out


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


def test_format_done_renders_pipeline_notes() -> None:
    out = mcp_server._format_gather_status(
        {
            "corpus": "httpbis", "state": "done",
            "started": "2026-06-03T12:00:00Z", "finished": "2026-06-03T12:03:20Z",
            "notes": ["Auto-tracked 1 GitHub repo(s) from discovery: httpwg/http-extensions."],
        }
    )
    assert "**httpbis** — done" in out
    # Notes render on their own indented line below the status line.
    assert "\n  - Auto-tracked 1 GitHub repo(s)" in out
    assert "httpwg/http-extensions" in out


def test_format_interrupted_omits_growing_elapsed() -> None:
    out = mcp_server._format_gather_status(
        {"corpus": "tls", "state": "interrupted",
         "started": "2026-06-03T12:00:00Z", "finished": None}
    )
    assert "**tls** — interrupted" in out
    assert "re-run" in out
    # No elapsed token (it would grow on every poll); elapsed always has a
    # digit, and nothing else in an interrupted line does.
    assert not any(ch.isdigit() for ch in out)


# --- tool_suggest_github_repos --------------------------------------------


def test_suggest_github_repos_empty_corpus() -> None:
    assert "Provide a Working Group shortname" in mcp_server.tool_suggest_github_repos("")


def test_suggest_github_repos_invalid_name() -> None:
    out = mcp_server.tool_suggest_github_repos("../etc")
    assert "not a valid corpus name" in out


def test_suggest_github_repos_renders_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ietf_llm.gather.repo_discovery as rd  # pylint: disable=import-outside-toplevel

    cand = rd.RepoCandidate("httpwg/http-extensions", True, 101, "", False, False)
    cand.draft_files = ["draft-ietf-httpbis-x.md"]
    cand.wg_draft_matches = ["draft-ietf-httpbis-x"]
    cand.issues_active = True
    monkeypatch.setattr(
        rd,
        "discover_group_repos",
        lambda wg, verbose=None: rd.DiscoveryResult(
            wg=wg, candidates=[cand], high_confidence=["httpwg/http-extensions"]
        ),
    )
    out = mcp_server.tool_suggest_github_repos("httpbis")
    assert "httpwg/http-extensions" in out
    assert 'github=["httpwg/http-extensions"]' in out
