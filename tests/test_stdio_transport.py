"""The threaded-writer stdio transport, driven as a real subprocess.

A reader that cannot parse what the SDK's wire format looks like fails *silently*:
the bad line is handed to the server as an exception, nothing is answered, and
the client times out. So these tests speak JSON-RPC to the transport for real,
in both protocol eras, rather than checking its pieces in isolation.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from typing import Any, Dict, List

import pytest

from ietf_llm import __version__

_TIMEOUT = 20

_SERVER = """
import anyio
from mcp.server.mcpserver import MCPServer
from ietf_llm.mcp import stdio

server = MCPServer("ietf-llm", instructions="ROUTING-BRAIN", version={version!r})

@server.tool()
def echo(x: str) -> str:
    "Echo."
    return x

async def main():
    async with stdio.stdio_server_threaded_writer() as (read, write):
        lowlevel = server._lowlevel_server
        await lowlevel.run(read, write, lowlevel.create_initialization_options())

anyio.run(main)
"""

_ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _exchange(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Send `requests`, return one response per request that has an id.

    A transport that cannot read the requests answers nothing, so the read is
    bounded: a hang must fail the test, not stall the suite."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER.format(version=__version__)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
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
            return [json.loads(lines.get(timeout=_TIMEOUT)) for _ in range(wanted)]
        except queue.Empty:
            pytest.fail(f"no response within {_TIMEOUT}s: the transport is not reading")
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_legacy_initialize_and_tool_call() -> None:
    init, call = _exchange(
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
    assert init["result"]["serverInfo"]["version"] == __version__
    assert init["result"]["instructions"] == "ROUTING-BRAIN"
    assert call["result"]["content"][0]["text"] == "hi"


def test_2026_07_28_discover_and_tool_call() -> None:
    discover, call = _exchange(
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
    # The routing brain and version must survive the era without `initialize`.
    assert discover["result"]["instructions"] == "ROUTING-BRAIN"
    info = discover["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert info["version"] == __version__
    assert call["result"]["content"][0]["text"] == "hi"
