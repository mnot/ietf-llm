"""R18: health/readiness endpoint. A GET /health route reports readiness
(index dir mounted + usable) with no upstream embed call, so a fronting
load balancer / orchestrator can probe the container.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from ietf_llm import mcp_server


class _FakeServer:
    """streamable_http_app() returns a plain Starlette app, so /health can
    be exercised without the real MCP session-manager lifespan."""

    def streamable_http_app(self) -> Any:
        return Starlette(routes=[])


def test_readiness_ready(isolated_home):
    ready, detail = mcp_server._readiness()
    assert ready is True
    assert detail["index_dir_usable"] is True
    assert "embed_endpoint_configured" in detail


def test_readiness_not_ready_when_index_dir_missing(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_index_dir", lambda: "/no/such/dir/xyzzy")
    ready, detail = mcp_server._readiness()
    assert ready is False
    assert detail["index_dir_usable"] is False


def test_readiness_reports_remote_endpoint(monkeypatch, isolated_home):
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    _, detail = mcp_server._readiness()
    assert detail["embed_endpoint_configured"] is True


def test_health_route_ok(isolated_home):
    client = TestClient(mcp_server._http_app(_FakeServer()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_route_unavailable(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_index_dir", lambda: "/no/such/dir/xyzzy")
    client = TestClient(mcp_server._http_app(_FakeServer()))
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"
