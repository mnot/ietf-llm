"""Boot-time config validation for the HTTP serve path (issue #46).

Transport is not gated: HTTP + in-session gather is a supported trusted-box
shape (#41). We hard-refuse only configs that cannot work (contradictory
knobs, missing prerequisites), warn (never block) on exposure without auth,
and always log a posture banner. These tests drive the pure checker
(`_serve_config_problems`), the posture, the loopback heuristic, and that
`_validate_serve_config` raises only on a hard error.
"""

from __future__ import annotations
from ietf_llm import mcp

import pytest



@pytest.fixture(autouse=True)
def _clear_serve_env(monkeypatch):
    # Start every test from a known-empty knob set; tests opt knobs in.
    for var in (
        "IETF_LLM_ENABLE_GATHER",
        "IETF_LLM_INDEX_IMMUTABLE",
        "IETF_LLM_EMBED_MODEL",
        "IETF_LLM_EMBED_BASE_URL",
        "IETF_LLM_NO_EMBED",
        "IETF_LLM_MCP_HOST",
        "IETF_LLM_MCP_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _errors(host: str = "127.0.0.1") -> list[str]:
    return mcp.serve._serve_config_problems(host)[0]


def _warnings(host: str = "127.0.0.1") -> list[str]:
    return mcp.serve._serve_config_problems(host)[1]


# --- the clean baseline -----------------------------------------------------


def test_default_loopback_config_is_clean(isolated_home):
    # Defaults: gather off, local default model, bound to loopback.
    errors, warnings = mcp.serve._serve_config_problems("127.0.0.1")
    assert errors == []
    assert warnings == []
    # And validation does not raise.
    mcp.serve._validate_serve_config("127.0.0.1", 8000)


# --- 1a. gather + immutable contradiction -----------------------------------


def test_gather_plus_immutable_refuses(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setenv("IETF_LLM_INDEX_IMMUTABLE", "1")
    # Stub torch present so only the 1a contradiction is in play here.
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: True)
    errs = _errors()
    assert len(errs) == 1
    assert "ENABLE_GATHER" in errs[0] and "INDEX_IMMUTABLE" in errs[0]
    with pytest.raises(SystemExit):
        mcp.serve._validate_serve_config("127.0.0.1", 8000)


def test_immutable_alone_is_fine(isolated_home, monkeypatch):
    # Immutable read replica without gather is the normal served shape.
    monkeypatch.setenv("IETF_LLM_INDEX_IMMUTABLE", "1")
    assert _errors() == []


# --- 1b. gather + local model + no torch ------------------------------------


def test_gather_local_model_without_torch_refuses(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: False)
    errs = _errors()
    assert any("torch is not importable" in e for e in errs)


def test_gather_local_model_with_torch_ok(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: True)
    assert _errors() == []


def test_gather_no_embed_skips_torch_check(isolated_home, monkeypatch):
    # --no-embed gather never reaches the embed step, so torch is moot.
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setenv("IETF_LLM_NO_EMBED", "1")
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: False)
    assert _errors() == []


def test_gather_remote_model_skips_torch_check(isolated_home, monkeypatch):
    # A remote model needs no torch; with an endpoint set it is clean.
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/bge")
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: False)
    assert _errors() == []


# --- 1c. remote model without endpoint --------------------------------------


def test_remote_model_without_base_url_refuses(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/bge")
    errs = _errors()
    assert any("IETF_LLM_EMBED_BASE_URL" in e for e in errs)


def test_remote_model_with_base_url_ok(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/bge")
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    assert _errors() == []


def test_remote_endpoint_check_is_independent_of_gather(isolated_home, monkeypatch):
    # gather off, but the read path still needs the endpoint.
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/bge")
    assert any("IETF_LLM_EMBED_BASE_URL" in e for e in _errors())


# --- 2. exposure warning (never blocks) -------------------------------------


def test_nonloopback_warns_but_does_not_block(isolated_home):
    errors, warnings = mcp.serve._serve_config_problems("0.0.0.0")
    assert errors == []
    assert len(warnings) == 1
    assert "authentication" in warnings[0]
    # A warning alone must not raise.
    mcp.serve._validate_serve_config("0.0.0.0", 8000)


def test_nonloopback_warning_mentions_gather_egress(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setattr(mcp.serve, "_torch_importable", lambda: True)
    warnings = _warnings("0.0.0.0")
    assert any("egress" in w for w in warnings)


@pytest.mark.parametrize(
    "host,loopback",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("127.5.5.5", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("example.com", False),
        ("::", False),
    ],
)
def test_loopback_heuristic(host, loopback):
    assert mcp.serve._is_loopback_host(host) is loopback


# --- 3. posture banner ------------------------------------------------------


def test_posture_reports_the_knobs(isolated_home, monkeypatch):
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/bge")
    posture = mcp.serve._serve_posture("0.0.0.0", 9000)
    assert posture["transport"] == "http"
    assert posture["bind"] == "0.0.0.0:9000"
    assert posture["gather"] == "on"
    assert posture["embed_backend"] == "remote"
    assert posture["embed_model"] == "openai-embed/bge"
    assert posture["index_immutable"] == "no"


def test_effective_embed_model_precedence(isolated_home, monkeypatch):
    # Env wins; default applies when unset.
    assert mcp.serve._effective_embed_model() == mcp.serve.DEFAULT_EMBED_MODEL
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/x")
    assert mcp.serve._effective_embed_model() == "openai-embed/x"
