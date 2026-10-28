"""The threaded-writer stdio transport, driven as a real subprocess.

A reader that cannot parse what the SDK's wire format looks like fails *silently*:
the bad line is handed to the server as an exception, nothing is answered, and
the client times out. So these tests speak JSON-RPC to the transport for real,
in both protocol eras, rather than checking its pieces in isolation.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ietf_llm import __version__

_TIMEOUT = 20

# Run through the production wiring (`_run_with_threaded_writer`), not a copy
# of it, so a rename in the SDK breaks this test rather than only real stdio.
_SERVER = """
import anyio
from mcp.server.mcpserver import MCPServer
from ietf_llm.mcp.server import _run_with_threaded_writer

server = MCPServer("ietf-llm", instructions="ROUTING-BRAIN", version={version!r})

@server.tool()
def echo(x: str) -> str:
    "Echo."
    return x

anyio.run(_run_with_threaded_writer, server)
"""

_ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


_MAIN = "from ietf_llm.mcp import main; main()"


def _exchange(
    requests: List[Dict[str, Any]],
    script: "str | None" = None,
    env: "Dict[str, str] | None" = None,
) -> Dict[int, Dict[str, Any]]:
    """Send `requests`, return the responses keyed by id.

    A transport that cannot read the requests answers nothing, so the read is
    bounded: a hang must fail the test, not stall the suite."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script or _SERVER.format(version=__version__)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None
    lines: "queue.Queue[str]" = queue.Queue()
    stdout = proc.stdout
    threading.Thread(
        target=lambda: [lines.put(line) for line in stdout], daemon=True
    ).start()
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        wanted = sum(1 for r in requests if "id" in r)
        try:
            responses = [json.loads(lines.get(timeout=_TIMEOUT)) for _ in range(wanted)]
        except queue.Empty:
            proc.kill()
            stderr = proc.stderr.read() if proc.stderr else ""
            pytest.fail(
                f"no response within {_TIMEOUT}s: the transport is not reading\n{stderr}"
            )
        return {response["id"]: response for response in responses}
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_legacy_initialize_and_tool_call() -> None:
    got = _exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"x": "hi"}},
            },
        ]
    )
    init, call = got[1], got[2]
    assert init["result"]["serverInfo"]["version"] == __version__
    assert init["result"]["instructions"] == "ROUTING-BRAIN"
    assert call["result"]["content"][0]["text"] == "hi"


def test_2026_07_28_discover_and_tool_call() -> None:
    got = _exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": _ENVELOPE},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"x": "hi"},
                    "_meta": _ENVELOPE,
                },
            },
        ]
    )
    discover, call = got[1], got[2]
    # The routing brain and version must survive the era without `initialize`.
    assert discover["result"]["instructions"] == "ROUTING-BRAIN"
    info = discover["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["version"] == __version__
    assert call["result"]["content"][0]["text"] == "hi"


def test_main_advertises_no_subscriptions(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Through the real `main()`: the tool list is static and there are no
    # resources, so nothing is advertised to subscribe to. A listen stream
    # held open by a client stops the HTTP server shutting down on SIGTERM.
    monkeypatch.delenv("IETF_LLM_MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "openai-embed/test")
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "http://127.0.0.1:9")
    got = _exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": _ENVELOPE},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "subscriptions/listen",
                "params": {"notifications": {"toolsListChanged": True}, "_meta": _ENVELOPE},
            },
        ],
        script=_MAIN,
        env=dict(os.environ),
    )
    result = got[1]["result"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == __version__
    caps = result["capabilities"]
    assert caps["tools"]["listChanged"] is False
    assert caps.get("resources", {}).get("subscribe", False) is False
    assert "error" in got[2]
