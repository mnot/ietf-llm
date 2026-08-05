"""RFC-series tools (search_rfcs, get_rfc) — thin wrappers over ietf_llm.rfcs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Optional

from pydantic import Field

from ..singletons.rfcs import is_stale_miss, render_rfc, render_search
from .common import _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _render_rfc_live(number: str) -> str:
    """`render_rfc`, but don't report a miss the mirror is merely too old to
    know about (the RFC9846 case: published after the last gather).

    A hit — and a miss inside the TTL — is answered offline, so the common
    path never touches the network. Only a stale miss spends a bounded live
    revalidation, under the gather gate: on a read-only replica, or when the
    fetch is throttled or fails, `render_rfc` still reports the stale miss
    honestly rather than as a bare negative.
    """
    if is_stale_miss(number):
        # pylint: disable-next=import-outside-toplevel
        from ..gather.sources.rfcs import revalidate_index

        revalidate_index()
    return render_rfc(number)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def search_rfcs(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        query: Annotated[
            str,
            Field(
                description=(
                    "Words matched against RFC titles and keywords. A bare "
                    "number short-circuits to that RFC."
                )
            ),
        ],
        status: Annotated[
            Optional[str], Field(description="`current` | `obsoleted`.")
        ] = None,
        stream: Annotated[
            Optional[str],
            Field(
                description=(
                    "`ietf` | `irtf` | `iab` | `independent` | `editorial` | "
                    "`legacy`."
                )
            ),
        ] = None,
        level: Annotated[
            Optional[str],
            Field(
                description=(
                    "`std` | `bcp` | `informational` | `experimental` | "
                    "`historic` | `unknown`."
                )
            ),
        ] = None,
        group: Annotated[
            Optional[str], Field(description="IETF working group acronym.")
        ] = None,
        limit: Annotated[int, Field(description="Rows to return.", ge=1)] = 50,
    ) -> str:
        """Search the **published RFC series** by words in titles and
        keywords, returning a compact markdown list. A bare RFC number
        (e.g. "9110") short-circuits to that single RFC.

        This is the whole-series index (every RFC, all streams), mirrored
        from rfc.fyi — distinct from `search_corpus`, which is semantic
        search *within one gathered Working Group*. Reach here for "find
        an RFC about X", "which RFC is X", "what's the status of RFC N";
        reach for `search_corpus` for a corpus's own discussion of a topic.

        Follow a hit with `get_rfc(number)` for full metadata and its
        reference graph.
        """
        return await _offload(render_search, query, status, stream, level, group, limit)

    @server.tool()
    async def get_rfc(
        number: Annotated[
            str, Field(description='RFC number or name — "9110" or "RFC9110".')
        ],
    ) -> str:
        """Full metadata for one RFC from the published series: title,
        status, stream, level, working group, keywords, what it
        obsoletes / is obsoleted by, its normative + informative
        references, how many RFCs cite it, and links to the text.

        This is catalogue metadata, not the document body — to read the
        prose, follow the text link in the output.

        Reads a local mirror of the RFC series. If the number is missing
        and that mirror is stale, it is refreshed live before answering,
        so a just-published RFC still resolves.
        """
        return await _offload(_render_rfc_live, number)
