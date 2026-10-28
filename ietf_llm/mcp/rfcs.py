"""RFC-series tools (search_rfc_index, get_rfc_info) — thin wrappers over ietf_llm.rfcs."""

from __future__ import annotations

import contextvars
import re
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from .. import serve_metrics
from ..log import LogLevel, log
from ..paths import DIR_DRAFTS
from ..store.blobs import parallel_each
from ..store.corpus import get_corpus_store
from ..singletons.rfcs import (
    is_stale_miss,
    render_rfc,
    render_search,
    working_group,
)
from .common import _list_wgs, _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


def _cached_body(number: str) -> Optional[Tuple[str, str]]:
    """`(corpus, relpath)` of a cached body for RFC `number`, or None.

    RFC bodies are not part of the `_rfc/` mirror — that is metadata only.
    They land on disk only when a corpus that published the RFC was gathered
    with `--rfcs`, as `drafts/rfcNNNN.txt`.

    Asks each gathered corpus whether it holds the file (`_corpus_has`), which on
    the cloud backend is answered from the version's manifest: exact, and
    without staging the version. The publishing WG goes first, since it is the
    overwhelmingly likely holder and a hit there ends the lookup; the rest are
    asked concurrently, because on a cold replica each is a round trip. Among
    them the first in sorted order wins, so the pointer doesn't move between
    calls. The caller reads the bytes with `read_file_section`, which is where
    a cloud version is staged — only for the corpus that has the file.
    """
    match = re.search(r"\d+", str(number))
    if not match:
        return None
    # The relpath goes back to the model as a `read_file_section` argument,
    # so it stays `/`-separated whatever the host separator is.
    relpath = f"{DIR_DRAFTS}/rfc{int(match.group(0))}.txt"
    corpora = sorted(_list_wgs())
    owner = working_group(number)
    if owner is not None and owner in corpora and _corpus_has(owner, relpath):
        return owner, relpath
    rest = [wg for wg in corpora if wg != owner]
    held: Dict[str, bool] = {}
    # A worker thread starts with an empty context, so a version pin (a
    # ContextVar) would not reach it; run each ask in a copy of ours.
    contexts = {wg: contextvars.copy_context() for wg in rest}

    def _ask(wg: str) -> None:
        held[wg] = contexts[wg].run(_corpus_has, wg, relpath)

    parallel_each(_ask, rest)
    for wg in rest:
        if held[wg]:
            return wg, relpath
    return None


def _corpus_has(wg: str, relpath: str) -> bool:
    """Whether `wg` holds `relpath` in its `files/` tree, asked of the store's
    `has_file` -- which never stages the version, unlike resolving its dir.

    A corpus that cannot answer is one that does not hold it. This only
    decorates a metadata lookup (`_body_note`), so a version that vanished
    mid-request, or a store hiccup on a corpus unrelated to the RFC, must not
    fail it; the note then says the body is not reachable from here.
    """
    try:
        return serve_metrics.timed_store(
            "has_file", lambda: get_corpus_store().has_file(wg, relpath)
        )
    except Exception as err:  # pylint: disable=broad-except
        log(f"has_file({wg!r}, {relpath!r}) failed: {err!r}", level=LogLevel.WARN)
        return False


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

        Follow a hit with `get_rfc_info(rfc)` for full metadata and its
        reference graph.
        """
        return await _offload(render_search, query, status, stream, level, group, limit)

    @server.tool()
    async def get_rfc_info(rfc: str) -> str:
        """Catalogue metadata and the reference graph for one RFC — **not
        the document text**, which is what the name says and what
        `get_rfc_section` is for.

        Full metadata for one RFC from the published series: title,
        status, stream, level, working group, keywords, what it
        obsoletes / is obsoleted by, its normative + informative
        references, how many RFCs cite it, and links to the text.

        `rfc` is an RFC number or name ("9110" or "RFC9110").

        **This is catalogue metadata, never the document body.** The last
        line of the output says which of two cases you are in: the body is
        cached in some corpus (it gives you the exact `read_file_section`
        call), or this server cannot reach it. In the second case do not
        characterise what the RFC says — you have its title and its
        reference graph, not its text. Note the claim is about reach, not
        existence: no gathered corpus on this server holds the body, but the
        RFC text itself is published.

        Reads a local mirror of the RFC series. If the number is missing
        and that mirror is stale, it is refreshed live before answering,
        so a just-published RFC still resolves.
        """
        return await _offload(_render_rfc_live, rfc)
