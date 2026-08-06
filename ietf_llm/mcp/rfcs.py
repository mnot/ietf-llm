"""RFC-series tools (search_rfcs, get_rfc) — thin wrappers over ietf_llm.rfcs."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Optional, Tuple

from ..paths import DIR_DRAFTS, drafts_dir
from ..singletons.rfcs import is_stale_miss, render_rfc, render_search
from .common import _files_dir, _list_wgs, _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _cached_body(number: str) -> Optional[Tuple[str, str]]:
    """`(corpus, relpath)` of a cached body for RFC `number`, or None.

    RFC bodies are not part of the `_rfc/` mirror — that is metadata only.
    They land on disk only when a corpus that published the RFC was gathered
    with `--rfcs`, as `drafts/rfcNNNN.txt`. Same read-only, offline scan
    `_find_latest_draft_file` does for drafts; corpora are visited in a
    stable order so the pointer doesn't move between calls.
    """
    match = re.search(r"\d+", str(number))
    if not match:
        return None
    filename = f"rfc{int(match.group(0))}.txt"
    # The relpath goes back to the model as a `read_file_section` argument,
    # so it stays `/`-separated whatever the host separator is.
    relpath = f"{DIR_DRAFTS}/{filename}"
    for wg in sorted(_list_wgs()):
        try:
            cache = _files_dir(wg)
        except FileNotFoundError:
            continue
        if os.path.isfile(os.path.join(drafts_dir(cache), filename)):
            return wg, relpath
    return None


def _body_note(number: str) -> str:
    """The body-availability line appended to a rendered RFC entry.

    `get_rfc` answers from a catalogue of titles and references; it has never
    returned the document text. Left unsaid, a well-formed metadata response
    reads as "here is the RFC", and a caller that needed to quote a section
    instead reasons from memory — which is how a review came to rule out a
    finding on RFC 8820 by recalling the wrong part of it (issue #218). So
    say which of the two cases this is, every time.
    """
    found = _cached_body(number)
    if found is not None:
        corpus, relpath = found
        return (
            f"- Body: cached in `{corpus}` — "
            f'`read_file_section("{corpus}", "{relpath}")`'
        )
    return (
        "- Body: **not available offline.** Everything above is catalogue "
        "metadata, not the document text — do not characterise what this RFC "
        "says from it. Quote it from the Text link above, or gather a corpus "
        "that published it (`ietf-llm <wg> --rfcs`) and re-run."
    )


def _render_rfc_live(number: str) -> str:
    """`render_rfc`, but don't report a miss the mirror is merely too old to
    know about (the RFC9846 case: published after the last gather), and stamp
    whether the body text is actually reachable (see `_body_note`).

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
    rendered = render_rfc(number)
    # Only a rendered entry gets the note; a miss / ungathered-index message
    # is not a metadata response anyone could mistake for the document.
    if not rendered.startswith("## "):
        return rendered
    return f"{rendered}\n{_body_note(number)}"


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

        `number` is an RFC number or name ("9110" or "RFC9110").

        **This is catalogue metadata, never the document body.** The last
        line of the output says which of two cases you are in: the body is
        cached in some corpus (it gives you the exact `read_file_section`
        call), or it is not reachable offline at all. In the second case do
        not characterise what the RFC says — you have its title and its
        reference graph, not its text.

        Reads a local mirror of the RFC series. If the number is missing
        and that mirror is stale, it is refreshed live before answering,
        so a just-published RFC still resolves.
        """
        return await _offload(_render_rfc_live, number)
