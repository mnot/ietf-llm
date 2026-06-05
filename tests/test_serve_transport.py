"""HTTP-transport serve knobs: the configurable Host/Origin allow-list for
DNS-rebinding protection (#41). The validation itself lives in the MCP
library; these tests cover that our env knobs wire it on and off correctly,
plus an end-to-end check that a disallowed Host is rejected.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from ietf_llm import mcp_server


def test_csv_env_splits_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_X", " a.example , , b.example:* ")
    assert mcp_server._csv_env("IETF_LLM_X") == ["a.example", "b.example:*"]
    monkeypatch.delenv("IETF_LLM_X", raising=False)
    assert mcp_server._csv_env("IETF_LLM_X") == []


def test_transport_security_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_MCP_ALLOWED_HOSTS", raising=False)
    assert mcp_server._transport_security_settings() is None


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


def test_posture_reports_stateless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_MCP_STATELESS", raising=False)
    assert mcp_server._serve_posture("0.0.0.0", 8000)["stateless"] == "yes"
    monkeypatch.setenv("IETF_LLM_MCP_STATELESS", "0")
    assert mcp_server._serve_posture("0.0.0.0", 8000)["stateless"] == "no"


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
