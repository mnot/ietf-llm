"""RFC-series tools (search_rfc_index, get_rfc_info) — thin wrappers over ietf_llm.rfcs."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Optional, Tuple

from ..paths import DIR_DRAFTS, drafts_dir
from ..store.corpus import VersionVanished
from ..singletons.rfcs import (
    is_stale_miss,
    render_rfc,
    render_search,
    working_group,
)
from .common import _files_dir, _list_wgs, _materialised_files_dir, _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _has_body(cache: Optional[str], filename: str) -> bool:
    return cache is not None and os.path.isfile(
        os.path.join(drafts_dir(cache), filename)
    )


def _cached_body(number: str) -> Optional[Tuple[str, str]]:
    """`(corpus, relpath)` of a cached body for RFC `number`, or None.

    RFC bodies are not part of the `_rfc/` mirror — that is metadata only.
    They land on disk only when a corpus that published the RFC was gathered
    with `--rfcs`, as `drafts/rfcNNNN.txt`.

    Two lookups, because on the cloud backend resolving a corpus's files dir
    *materialises* that version's blobs onto scratch:

    1. The publishing WG's own corpus, resolved the forcing way. That is the
       overwhelmingly likely holder and it is exactly one corpus, so it is
       worth the one materialisation.
    2. Every gathered corpus, resolved the *non*-forcing way — an RFC can be
       cached under a corpus that did not publish it, and the WG is unknown
       for the legacy and independent streams. Sweeping this with the forcing
       resolver would download the whole fleet on a metadata lookup.

    So on cloud a body in an unstaged corpus reads as absent. That is the
    honest answer for a read path that must not fetch, and `_body_note` says
    "not reachable from here" rather than claiming it does not exist.
    Corpora are visited in a stable order so the pointer doesn't move between
    calls; on the local backend both resolvers are the same directory and the
    result is exactly as before.
    """
    match = re.search(r"\d+", str(number))
    if not match:
        return None
    filename = f"rfc{int(match.group(0))}.txt"
    # The relpath goes back to the model as a `read_file_section` argument,
    # so it stays `/`-separated whatever the host separator is.
    relpath = f"{DIR_DRAFTS}/{filename}"
    corpora = sorted(_list_wgs())
    owner = working_group(number)
    if owner and owner in corpora:
        try:
            if _has_body(_files_dir(owner), filename):
                return owner, relpath
        # A version that vanished mid-request (VersionVanished) or resolves to
        # nothing (FileNotFoundError) is a reason to fall through to the sweep,
        # not to fail a metadata lookup outright.
        except (FileNotFoundError, VersionVanished):
            pass
    for wg in corpora:
        if wg != owner and _has_body(_materialised_files_dir(wg), filename):
            return wg, relpath
    return None


def _body_note(number: str) -> str:
    """The body-availability line appended to a rendered RFC entry.

    `get_rfc_info` answers from a catalogue of titles and references; it has never
    returned the document text. Left unsaid, a well-formed metadata response
    reads as "here is the RFC", and a caller that needed to quote a section
    instead reasons from memory — which is how a review came to rule out a
    finding on RFC 8820 by recalling the wrong part of it (issue #218). So
    say which of the three cases this is, every time.

    Since #230 there is usually a good answer: the RFC full-text corpus holds
    every published RFC, so the note points at `get_rfc_section` rather than
    apologising. The older cases remain — a corpus that gathered the body
    with `--rfcs`, and nothing at all — because neither the text corpus nor a
    given corpus is guaranteed to be installed.
    """
    if _rfc_text_available(number):
        return (
            f'- Body: **`get_rfc_section("{number}")`** for the outline, or '
            f'`get_rfc_section("{number}", "<section>")` to read one. '
            "Everything above is catalogue metadata, not the document text."
        )
    found = _cached_body(number)
    if found is not None:
        corpus, relpath = found
        return (
            f"- Body: cached in `{corpus}` — "
            f'`read_file_section("{corpus}", "{relpath}")`'
        )
    return (
        "- Body: **not reachable from here.** Everything above is catalogue "
        "metadata, not the document text — do not characterise what this RFC "
        "says from it. Quote it from the Text link above, or install the RFC "
        "full-text corpus (any `ietf-llm <corpus>` run pulls it) and re-run."
    )


def _rfc_text_available(number: str) -> bool:
    """Whether the RFC full-text corpus holds this RFC.

    A cheap existence probe, not a read: the note only needs to know which
    call to recommend.
    """
    # pylint: disable-next=import-outside-toplevel
    from ..embeddings.storage import section_outline

    # pylint: disable-next=import-outside-toplevel
    from .rfc_text import RFC_CORPUS, _rfc_file

    file = _rfc_file(number)
    return bool(file) and bool(section_outline(RFC_CORPUS, str(file)))


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
    async def search_rfc_index(  # pylint: disable=too-many-arguments,too-many-positional-arguments
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

        Follow a hit with `get_rfc_info(number)` for full metadata and its
        reference graph.
        """
        return await _offload(render_search, query, status, stream, level, group, limit)

    @server.tool()
    async def get_rfc_info(number: str) -> str:
        """Catalogue metadata and the reference graph for one RFC — **not
        the document text**, which is what the name says and what
        `get_rfc_section` is for.

        Full metadata for one RFC from the published series: title,
        status, stream, level, working group, keywords, what it
        obsoletes / is obsoleted by, its normative + informative
        references, how many RFCs cite it, and links to the text.

        `number` is an RFC number or name ("9110" or "RFC9110").

        **This is catalogue metadata, never the document body.** The last
        line of the output says which of two cases you are in: the body is
        cached in some corpus (it gives you the exact `read_file_section`
        call), or this server cannot reach it. In the second case do not
        characterise what the RFC says — you have its title and its
        reference graph, not its text. Note the claim is about reach, not
        existence: on a shared deployment the body may well be published
        somewhere in the fleet without being readable from here.

        Reads a local mirror of the RFC series. If the number is missing
        and that mirror is stale, it is refreshed live before answering,
        so a just-published RFC still resolves.
        """
        return await _offload(_render_rfc_live, number)
