"""Semantic search over the full text of the RFC series, and section reads.

Two tools over the imported RFC corpus (issue #230), both read-only and
offline:

  `search_rfc_text`   what the documents *say* — semantic, passage-level
  `get_rfc_section`   you already have the citation — read that section

Three shaping decisions worth stating.

**Results are sections, not chunks.** A chunk is an embedding unit, not a
citable one: rows store text trimmed of the chunker's carried-forward
overlap, and 23% of trimmed rows begin mid-sentence. Assembling a section
from its rows restores the original wrapping exactly, so what a caller sees
begins where the section begins.

**Documents are ranked, not passages.** Ordering an RFC by its single best
section lets one that mentions a topic tie one that is about it; see
`singletons.rfc_rank` for the scheme and the numbers.

**Obsoleted RFCs are marked, never hidden.** The corpus is the whole series,
so a caching query surfaces RFC 2616 alongside 9111. Dropping superseded
documents would be wrong — sometimes the old text is what you want — but
letting one be cited as current is worse, so every hit carries its status.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..embeddings.search import search as _search
from ..embeddings.storage import (
    META_SOURCE_BUILD,
    META_SOURCE_COMMIT,
    read_meta,
    section_outline,
    section_rows,
)
from ..singletons.rfc_rank import rank_documents
from ..singletons.rfcs import rfc_num_to_name
from .common import _offload

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover

#: The corpus name the imported RFC series is installed under.
RFC_CORPUS = "rfcs"

#: Chunks retrieved before aggregation. Deep enough that a document with
#: several relevant sections can show them, shallow enough that ranking
#: stays cheap.
_RETRIEVE_CHUNKS = 200

#: Sections shown per RFC in a result list.
_SECTIONS_PER_RFC = 3

_SNIPPET_CHARS = 320

#: `Section 7.2`, `§7.2`, `7.2.`, `sec 7.2`, `Appendix A` all mean the label
#: the index stores, which is bare: `7.2`, `A`.
_ASK_RE = re.compile(
    r"^\s*(?:§+\s*|sec(?:tion)?\.?\s+|appendix\s+|annex\s+)?([0-9A-Za-z][0-9A-Za-z.]*?)\.?\s*$",
    re.IGNORECASE,
)


def _normalise_section(ask: str) -> Optional[str]:
    """Turn what a caller typed into the label the index stores."""
    match = _ASK_RE.match(ask or "")
    if not match:
        return None
    label = match.group(1)
    # Appendix labels are upper-case in the source; numeric ones are digits
    # and dots, which `upper()` leaves alone.
    return label.upper() if label[:1].isalpha() else label


#: An RFC identifier as the index stores it: digits, and for two chunks in
#: the whole corpus a trailing letter (`17a`). Accepts "9110", "RFC9110",
#: "rfc 9110".
_NUMBER_RE = re.compile(r"^\s*(?:rfc[\s-]*)?(\d+)([a-z])?\s*$", re.IGNORECASE)


def _rfc_file(number: str) -> Optional[str]:
    """The corpus filename for an RFC number.

    The suffix is preserved rather than parsed away. `format.py` and the
    importer take care to keep `rfc` a string precisely because of `17a`, and
    coercing to `int` here would send `get_rfc_section("17a")` to `rfc17.txt`
    — undoing that care at the last step, for the only two chunks it exists
    for.
    """
    match = _NUMBER_RE.match(str(number))
    if not match:
        return None
    return f"rfc{int(match.group(1))}{(match.group(2) or '').lower()}.txt"


def _status_note(number: str) -> str:
    """`(obsoleted by RFC 9111)` or "" — the citation guard.

    Read from the `_rfc/` metadata mirror, which is a separate singleton from
    the text corpus, so this stays accurate between corpus refreshes.
    """
    # pylint: disable-next=import-outside-toplevel
    from ..singletons.rfcs import _load

    data = _load()
    if data is None:
        return ""
    name = rfc_num_to_name(number)
    by = data.obsoleted_by.get(name) or []
    if by:
        return f"  **obsoleted by {', '.join(by)}**"
    return ""


def _title_of(number: str) -> str:
    # pylint: disable-next=import-outside-toplevel
    from ..singletons.rfcs import _load

    data = _load()
    if data is None:
        return ""
    return str((data.rfcs.get(rfc_num_to_name(number)) or {}).get("title", ""))


def _citations_of(number: str) -> int:
    # pylint: disable-next=import-outside-toplevel
    from ..singletons.rfcs import _load

    data = _load()
    if data is None:
        return 0
    return len(data.inbound_refs(rfc_num_to_name(number), True))


def _number_of(file: str) -> str:
    return file[3:-4] if file.startswith("rfc") and file.endswith(".txt") else file


def _provenance() -> str:
    """One line naming the snapshot this text came from.

    A citation should be pinnable to a version of the source, and this corpus
    is a monthly snapshot of a series that keeps growing: without the build
    id a caller cannot tell an August corpus from a September one, and cannot
    tell "this RFC does not exist" from "this snapshot predates it". The
    build id is also the upstream release tag, so it resolves to the exact
    artifact this was assembled from.
    """
    meta = read_meta(RFC_CORPUS, (META_SOURCE_BUILD, META_SOURCE_COMMIT))
    build = meta.get(META_SOURCE_BUILD)
    if not build:
        return ""
    stamp = build
    if len(build) >= 8 and build[:8].isdigit():
        stamp = f"{build[:4]}-{build[4:6]}-{build[6:8]}"
    commit = meta.get(META_SOURCE_COMMIT, "")
    tail = f", rfc.fyi {commit[:8]}" if commit else ""
    return (
        f"Text from the RFC-series snapshot built {stamp} "
        f"(`index-{build}`{tail}). An RFC published since then is not in it — "
        "`get_rfc_info` is the authority on whether one exists."
    )


def section_text(number: str, section: Optional[str]) -> Optional[str]:
    """Assemble one section from its rows, in document order.

    Rows hold text trimmed of the chunker's overlap, so concatenating them
    reproduces the section as published — which is why this exists rather
    than a caller reading a single chunk.
    """
    file = _rfc_file(number)
    if file is None:
        return None
    rows = section_rows(RFC_CORPUS, file, section)
    if not rows:
        return None
    return "\n".join(text for _idx, text in rows).strip()


def _outline(number: str) -> List[Tuple[str, str, int]]:
    """`(section, title, characters)` for every indexed section of an RFC."""
    file = _rfc_file(number)
    if file is None:
        return []
    return section_outline(RFC_CORPUS, file)


def _render_outline(number: str, rows: List[Tuple[str, str, int]]) -> str:
    lines = [
        f"## RFC {number} — indexed sections{_status_note(number)}",
        "",
        "| Section | Title | Chars |",
        "|---|---|---|",
    ]
    lines += [f"| {sec} | {title} | {size:,} |" for sec, title, size in rows]
    lines.append("")
    lines.append(
        "_Front and back matter (status, copyright, authors' addresses, the "
        "reference list) is not indexed, so a gap here means that section "
        "exists in the document but is not searchable._"
    )
    provenance = _provenance()
    if provenance:
        lines.append("")
        lines.append(f"_{provenance}_")
    return "\n".join(lines)


def tool_search_rfc_text(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    query: str,
    rfc: Optional[str] = None,
    limit: int = 10,
    sections_per_rfc: int = _SECTIONS_PER_RFC,
    snippet_chars: int = _SNIPPET_CHARS,
) -> str:
    file_pattern = _rfc_file(rfc) if rfc else None
    if rfc and file_pattern is None:
        return f"'{rfc}' is not an RFC number."
    hits = _search(
        RFC_CORPUS,
        query,
        k=_RETRIEVE_CHUNKS,
        file_pattern=file_pattern,
        diversify=False,
    )
    if not hits:
        return (
            f"No hits for {query!r} in the RFC series text.\n\n"
            "If the RFC full-text corpus is not installed on this server, "
            "semantic search over RFC text is unavailable here — run "
            "`ietf-llm --init` to install it. `search_rfc_index` searches "
            "titles and keywords either way."
        )

    ranked = rank_documents(
        query,
        hits,
        doc_of=lambda h: _number_of(h.file),
        score_of=lambda h: h.score,
        title_of=_title_of,
        citations_of=_citations_of,
    )

    out: List[str] = [f"# RFC full-text search: {query!r}", ""]
    for entry in ranked[:limit]:
        number = entry.doc
        title = _title_of(number)
        # The title comes from the `_rfc/` metadata mirror, a separate
        # singleton — on a machine that has the text corpus but has not
        # mirrored the metadata yet, it is empty, and a dangling em-dash is
        # worse than no title at all.
        heading = f"## RFC {number} — {title}" if title else f"## RFC {number}"
        out.append(f"{heading}{_status_note(number)}")
        seen: Dict[Optional[str], Any] = {}
        for hit in entry.hits:
            if hit.section not in seen:
                seen[hit.section] = hit
            if len(seen) >= sections_per_rfc:
                break
        for sec, hit in seen.items():
            label = f"§{sec}" if sec else "(unsectioned)"
            body = section_text(number, sec) or hit.snippet
            snippet = " ".join(body.split())[:snippet_chars]
            out.append(f"- **{label} {hit.title}** — score {hit.score:.3f}")
            out.append(f"  {snippet}…")
        out.append("")
    out.append(
        f"_{len(ranked)} RFCs matched; showing {min(limit, len(ranked))}. "
        "Snippets are the opening of each section, not the passage that "
        "matched — read the section with `get_rfc_section` before quoting "
        "it._"
    )
    provenance = _provenance()
    if provenance:
        out.append("")
        out.append(f"_{provenance}_")
    return "\n".join(out)


def tool_get_rfc_section(number: str, section: Optional[str] = None) -> str:
    rows = _outline(number)
    if not rows:
        return (
            f"No indexed text for RFC {number}. Either the RFC-series text "
            "corpus is not installed on this server — `ietf-llm --init` "
            "installs it — or that RFC is not in it (a very old RFC may "
            "carry no section headings at all)."
        )
    if not section:
        return _render_outline(number, rows)

    label = _normalise_section(section)
    if label is None:
        return f"'{section}' is not a section label."
    wanted = [r for r in rows if r[0] == label or r[0].startswith(label + ".")]
    if not wanted:
        # The caller's citation may be real but unindexed (front/back matter),
        # or simply wrong. Showing what *is* there answers both without
        # guessing which.
        return f"RFC {number} has no indexed section {label}.\n\n" + _render_outline(
            number, rows
        )
    parts = [f"# RFC {number} §{label}{_status_note(number)}", ""]
    for sec, title, _size in wanted:
        body = section_text(number, sec)
        if not body:
            continue
        parts.append(f"## {sec}. {title}")
        parts.append("")
        parts.append(body)
        parts.append("")
    provenance = _provenance()
    if provenance:
        parts.append(f"_{provenance}_")
    return "\n".join(parts)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def search_rfc_text(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        query: str,
        rfc: Optional[str] = None,
        limit: int = 10,
        sections_per_rfc: int = _SECTIONS_PER_RFC,
        snippet_chars: int = _SNIPPET_CHARS,
    ) -> str:
        """Search **what RFCs say**, semantically, over the full text of the
        whole series — returning the RFCs that answer the query, each with
        the sections that matched.

        Distinct from its two neighbours, and the difference matters:

          - `search_rfc_index` searches **titles and keywords** only. Reach
            for it to find *which* document something is ("which RFC is
            HTTP caching", "RFCs from the TLS WG"), and to filter by status
            / stream / level / group.
          - `search_corpus` searches **one gathered Working Group's own
            discussion** — mail, issues, drafts. Reach for it for what a
            group said about a topic.
          - This tool searches the **published text of every RFC**. Reach
            for it when the question is what the specifications actually
            say: "when must a cache not store a response", "how does QUIC
            do key update".

        `rfc` narrows to a single document, which turns this into
        search-within-this-RFC — useful when the document is large.

        Results are **sections**, ranked by document. Snippets are the
        opening of each matched section, not the passage that matched, so
        follow a hit with `get_rfc_section` before quoting.

        Obsoleted RFCs are included and **marked**; the series is the whole
        historical record, so check the marker before citing anything as
        current.
        """
        return await _offload(
            tool_search_rfc_text, query, rfc, limit, sections_per_rfc, snippet_chars
        )

    @server.tool()
    async def get_rfc_section(number: str, section: Optional[str] = None) -> str:
        """Read one section of an RFC from the local text corpus, offline.

        `number` is an RFC number ("9111"). `section` is a label as cited —
        `7.2`, `§7.2`, `Section 7.2` and `Appendix A` are all accepted. A
        parent label returns it and everything beneath it, so `section="7"`
        gives §7, §7.1, §7.2…

        With no `section`, returns that RFC's indexed outline: every section
        with its title and size. That is the way to find the citation you
        want, and it is also what a miss returns — so a wrong or unindexed
        label shows you what *is* available instead of a bare negative.

        **Not every section is indexed.** Front and back matter — status of
        this memo, copyright, authors' addresses, the reference list — is
        excluded, so a gap in the numbering means "exists but not
        searchable", not "does not exist".

        This is the document text, so it *is* quotable — unlike `get_rfc_info`,
        which returns catalogue metadata only.
        """
        return await _offload(tool_get_rfc_section, number, section)
