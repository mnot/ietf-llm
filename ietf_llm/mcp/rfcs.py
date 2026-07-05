"""RFC-series tools (search_rfcs, get_rfc) — thin wrappers over ietf_llm.rfcs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..rfcs import render_rfc, render_search
from .common import _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def register(server: "FastMCP") -> None:
    @server.tool()
    async def search_rfcs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        query: str,
        status: Optional[str] = None,
        stream: Optional[str] = None,
        level: Optional[str] = None,
        group: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """Search the **published RFC series** by words in titles and
        keywords, returning a compact markdown list. A bare RFC number
        (e.g. "9110") short-circuits to that single RFC.

        This is the whole-series index (every RFC, all streams), mirrored
        from rfc.fyi — distinct from `search_corpus`, which is semantic
        search *within one gathered Working Group*. Reach here for "find
        an RFC about X", "which RFC is X", "what's the status of RFC N";
        reach for `search_corpus` for a corpus's own discussion of a topic.

        Optional filters narrow the result set:
          - `status`: `current` | `obsoleted`
          - `stream`: `ietf` | `irtf` | `iab` | `independent` |
            `editorial` | `legacy`
          - `level`: `std` | `bcp` | `informational` | `experimental` |
            `historic` | `unknown`
          - `group`: an IETF working group acronym.
          - `limit`: max results (default 50).

        Follow a hit with `get_rfc(number)` for full metadata and its
        reference graph.
        """
        return await _offload(render_search, query, status, stream, level, group, limit)

    @server.tool()
    async def get_rfc(number: str) -> str:
        """Full metadata for one RFC from the published series: title,
        status, stream, level, working group, keywords, what it
        obsoletes / is obsoleted by, its normative + informative
        references, how many RFCs cite it, and links to the text.

        `number` is an RFC number or name ("9110" or "RFC9110"). This is
        catalogue metadata, not the document body — to read the prose,
        follow the text link in the output.
        """
        return await _offload(render_rfc, number)
