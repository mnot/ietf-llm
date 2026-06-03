"""R8: MCP transport selection. stdio is the default; the shared-server
deployment selects Streamable HTTP via IETF_LLM_MCP_TRANSPORT, bound to a
configurable host/port. No socket is bound -- uvicorn.run is stubbed.
"""

from __future__ import annotations

import uvicorn
import pytest
from starlette.applications import Starlette

from ietf_llm import mcp_server


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "stdio"),
        ("", "stdio"),
        ("stdio", "stdio"),
        ("http", "http"),
        ("HTTP", "http"),
        ("streamable-http", "http"),
        ("nonsense", "stdio"),
    ],
)
def test_resolve_transport(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("IETF_LLM_MCP_TRANSPORT", value)
    assert mcp_server._resolve_transport() == expected


class _FakeServer:
    """streamable_http_app() returns a real Starlette app so _http_app can
    add the /health route to it (a plain object has no add_route)."""

    def __init__(self):
        self.app = Starlette(routes=[])

    def streamable_http_app(self):
        return self.app


def test_run_http_binds_configured_host_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, host, port, **kw: captured.update(app=app, host=host, port=port),
    )
    monkeypatch.setenv("IETF_LLM_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("IETF_LLM_MCP_PORT", "9001")
    srv = _FakeServer()
    mcp_server._run_http(srv)
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9001
    # The served app is the streamable-HTTP app (now carrying /health).
    assert captured["app"] is srv.app


def test_run_http_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, host, port, **kw: captured.update(host=host, port=port),
    )
    monkeypatch.delenv("IETF_LLM_MCP_HOST", raising=False)
    monkeypatch.delenv("IETF_LLM_MCP_PORT", raising=False)
    mcp_server._run_http(_FakeServer())
    assert captured == {"host": "127.0.0.1", "port": 8000}


def test_run_http_bad_port_falls_back(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, host, port, **kw: captured.update(port=port),
    )
    monkeypatch.setenv("IETF_LLM_MCP_PORT", "notanint")
    mcp_server._run_http(_FakeServer())
    assert captured == {"port": 8000}
