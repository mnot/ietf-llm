"""HTTP-transport serve knobs: the configurable Host/Origin allow-list for
DNS-rebinding protection (#41). The validation itself lives in the MCP
library; these tests cover that our env knobs wire it on and off correctly,
plus an end-to-end check that a disallowed Host is rejected.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from ietf_llm import freshness, mcp_server


def test_csv_env_splits_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_X", " a.example , , b.example:* ")
    assert mcp_server._csv_env("IETF_LLM_X") == ["a.example", "b.example:*"]
    monkeypatch.delenv("IETF_LLM_X", raising=False)
    assert mcp_server._csv_env("IETF_LLM_X") == []


def test_transport_security_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset must EXPLICITLY disable protection, overriding the MCP library's
    # loopback-only default (which 421s a fronted public-hostname deployment).
    monkeypatch.delenv("IETF_LLM_MCP_ALLOWED_HOSTS", raising=False)
    settings = mcp_server._transport_security_settings()
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is False


def test_any_host_accepted_when_unset_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no allow-list, an arbitrary Host (what a proxy may forward) must not
    # be rejected — the regression this guards against is a silent 421.
    monkeypatch.delenv("IETF_LLM_MCP_ALLOWED_HOSTS", raising=False)
    server = FastMCP(
        "ietf-llm", instructions="x",
        transport_security=mcp_server._transport_security_settings(),
    )
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    with TestClient(server.streamable_http_app()) as client:
        resp = client.post(
            "/mcp", json=body, headers={**headers, "Host": "public.example.org"}
        )
    assert resp.status_code != 421


def test_transport_security_on_when_hosts_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_MCP_ALLOWED_HOSTS", "mcp.example.org, localhost:*")
    monkeypatch.setenv("IETF_LLM_MCP_ALLOWED_ORIGINS", "https://app.example.org")
    settings = mcp_server._transport_security_settings()
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["mcp.example.org", "localhost:*"]
    assert settings.allowed_origins == ["https://app.example.org"]


def test_posture_reports_host_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_MCP_ALLOWED_HOSTS", "mcp.example.org")
    assert mcp_server._serve_posture("0.0.0.0", 8000)["host_allowlist"] == (
        "mcp.example.org"
    )
    monkeypatch.delenv("IETF_LLM_MCP_ALLOWED_HOSTS", raising=False)
    assert mcp_server._serve_posture("0.0.0.0", 8000)["host_allowlist"] == "off"


def test_stateless_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_MCP_STATELESS", raising=False)
    assert mcp_server._stateless_http_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_stateless_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("IETF_LLM_MCP_STATELESS", value)
    assert mcp_server._stateless_http_enabled() is False


def test_stateless_setting_flows_to_fastmcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_MCP_STATELESS", raising=False)
    server = FastMCP(
        "ietf-llm", instructions="x",
        stateless_http=mcp_server._stateless_http_enabled(),
    )
    assert server.settings.stateless_http is True
    monkeypatch.setenv("IETF_LLM_MCP_STATELESS", "0")
    server = FastMCP(
        "ietf-llm", instructions="x",
        stateless_http=mcp_server._stateless_http_enabled(),
    )
    assert server.settings.stateless_http is False


def test_log_verbosity_default_quiet_on_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_LOG_LEVEL", raising=False)
    monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)
    assert mcp_server._log_verbosity() is mcp_server.Verbosity.QUIET


def test_log_verbosity_default_status_on_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_LOG_LEVEL", raising=False)
    monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", "http")
    assert mcp_server._log_verbosity() is mcp_server.Verbosity.STATUS


@pytest.mark.parametrize(
    "value,expected",
    [
        ("quiet", "QUIET"),
        ("status", "STATUS"),
        ("verbose", "VERBOSE"),
        ("progress", "VERBOSE"),
        ("2", "VERBOSE"),
        ("  Status  ", "STATUS"),
    ],
)
def test_log_verbosity_explicit_override(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    # An explicit level overrides the transport default in either direction.
    monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", "http")
    monkeypatch.setenv("IETF_LLM_LOG_LEVEL", value)
    assert mcp_server._log_verbosity().name == expected


def test_log_verbosity_unrecognised_falls_back_to_transport_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_LOG_LEVEL", "loud")
    monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", "http")
    assert mcp_server._log_verbosity() is mcp_server.Verbosity.STATUS
    monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)
    assert mcp_server._log_verbosity() is mcp_server.Verbosity.QUIET


def test_posture_reports_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_LOG_LEVEL", "verbose")
    assert mcp_server._serve_posture("0.0.0.0", 8000)["log_level"] == "verbose"


def test_posture_reports_stateless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_MCP_STATELESS", raising=False)
    assert mcp_server._serve_posture("0.0.0.0", 8000)["stateless"] == "yes"
    monkeypatch.setenv("IETF_LLM_MCP_STATELESS", "0")
    assert mcp_server._serve_posture("0.0.0.0", 8000)["stateless"] == "no"


@pytest.mark.parametrize(
    "transport_env,expected",
    [(None, True), ("stdio", True), ("http", False), ("streamable-http", False)],
)
def test_gather_default_tracks_transport(
    monkeypatch: pytest.MonkeyPatch, transport_env: "str | None", expected: bool
) -> None:
    # main() resolves the transport, then derives the in-session gather default
    # from it (stdio on, http off) before the registration gate. Reproduce that
    # one-line rule and confirm it lands on the resolved flag.
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    if transport_env is None:
        monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", transport_env)
    saved = freshness._GATHER_DEFAULT
    try:
        freshness.set_gather_default(mcp_server._resolve_transport() == "stdio")
        assert mcp_server._gather_enabled() is expected
    finally:
        freshness.set_gather_default(saved)


def test_posture_reports_gather(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_ENABLE_GATHER", raising=False)
    saved = freshness._GATHER_DEFAULT
    try:
        freshness.set_gather_default(True)
        assert mcp_server._serve_posture("0.0.0.0", 8000)["gather"] == "on"
        freshness.set_gather_default(False)
        assert mcp_server._serve_posture("0.0.0.0", 8000)["gather"] == "off"
    finally:
        freshness.set_gather_default(saved)


def test_disallowed_host_is_rejected_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_MCP_ALLOWED_HOSTS", "good.example.org")
    server = FastMCP(
        "ietf-llm",
        instructions="x",
        transport_security=mcp_server._transport_security_settings(),
    )
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    # The session manager validates inside its lifespan task group, so drive
    # the app as a context manager (which runs the lifespan).
    with TestClient(server.streamable_http_app()) as client:
        bad = client.post("/mcp", json=body, headers={**headers, "Host": "evil.example.org"})
        good = client.post("/mcp", json=body, headers={**headers, "Host": "good.example.org"})
    assert bad.status_code == 421  # Misdirected Request: Invalid Host header
    assert good.status_code != 421  # reaches the handler (a real Host)
