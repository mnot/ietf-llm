"""Introspection of the advertised MCP surface: the `instructions` field and
the serialized tool list a client receives before it asks anything.

This is what the budget gate weighs (`tests/test_mcp_surface_budget.py`) and
what the analysis tools read (`scripts/mcp_surface_report.py`,
`scripts/mcp_tool_similarity.py`). It lives in the package rather than beside
either so there is one construction path — and so `make lint typecheck` covers
it, which it does not for `tests/` or `scripts/`.

Nothing here runs at serve time; it is introspection over the same
`server.register_tools` the real server uses.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Tuple

from .. import freshness

#: The deployment shapes we ship, as (gather_enabled, deployment_mode).
#: `stdio` is the local server (gather + live Datatracker lookups registered);
#: `http` is the shared read-only replica. They advertise different tool sets
#: and different `instructions`, so anything measuring the surface must say
#: which one it means.
SHAPES: Dict[str, Tuple[bool, str]] = {
    "stdio": (True, "stdio"),
    "http": (False, "http"),
}


@dataclass(frozen=True)
class ToolSurface:
    """One tool as a client receives it."""

    name: str
    description: str
    schema: Dict[str, Any]

    def serialized(self) -> str:
        """The tool as it goes over the wire — description *and* schema, so the
        schema's own bulk is on the books and not just the prose."""
        return json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "inputSchema": self.schema,
            },
            separators=(",", ":"),
        )

    @property
    def chars(self) -> int:
        return len(self.serialized())

    @property
    def params(self) -> Dict[str, Dict[str, Any]]:
        props = self.schema.get("properties")
        return props if isinstance(props, dict) else {}


@contextlib.contextmanager
def session_shape(shape: str) -> Iterator[bool]:
    """Set the process-global session mode for `shape` and put it back.

    Both switches are module state the whole process shares, and the
    `instructions` text is composed from them (`common._session_section`), so
    measuring a shape means setting them — and restoring them, or the next
    caller measures the wrong server. Yields `gather_enabled`.
    """
    gather, mode = SHAPES[shape]
    before = (freshness.gather_enabled(), freshness.deployment_mode())
    freshness.set_gather_default(gather)
    freshness.set_deployment_mode(mode)
    try:
        yield gather
    finally:
        freshness.set_gather_default(before[0])
        freshness.set_deployment_mode(before[1])


def build_surface(
    shape: str, *, session_log_enabled: bool = False
) -> List[ToolSurface]:
    """Build the tool list `shape` advertises, without starting a server.

    Goes through `server.register_tools`, so a module added there is measured
    here for free — a copy of the registration list would have gone stale at
    the first new module.

    `session_log_enabled` defaults off to match what both shapes ship
    (`get_session_log` needs IETF_LLM_DEBUG_LOG=1).
    """
    # Imported here, not at module scope: `mcp.server.fastmcp` is the SDK
    # surface `server.main` guards with a diagnostic, and this module is
    # imported by tooling that should not depend on that import succeeding
    # any earlier than the server itself does.
    import anyio  # pylint: disable=import-outside-toplevel
    from mcp.server.fastmcp import FastMCP  # pylint: disable=import-outside-toplevel

    from .server import register_tools  # pylint: disable=import-outside-toplevel

    with session_shape(shape) as gather:
        server = FastMCP("ietf-llm")
        register_tools(
            server, gather_enabled=gather, session_log_enabled=session_log_enabled
        )
        tools = anyio.run(server.list_tools)
    return [
        ToolSurface(t.name, t.description or "", t.inputSchema)
        for t in sorted(tools, key=lambda t: t.name)
    ]


def instructions_text(shape: str) -> str:
    """The `instructions` field for `shape`, without the capability footer.

    The footer is machine-generated and embeds `__version__`, so including it
    would make a version bump move the measurement by a character.
    """
    from .server import (  # pylint: disable=import-outside-toplevel
        _load_server_instructions,
    )

    with session_shape(shape):
        return _load_server_instructions()
