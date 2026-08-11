"""Recovering section text from the index's byte ranges (#230).

Two claims carry the design and are asserted here. That a section's chunks
form one contiguous span, so slicing beats concatenating overlapping chunks.
And that pre-2019 pagination can be removed without touching the ASCII art
that RFC bodies are full of.
"""

from __future__ import annotations

import os
from typing import Any, List

from ietf_llm.rfcindex.format import ChunkMeta
from ietf_llm.rfcindex.text import (
    clean_section_text,
    read_section,
    section_spans,
)

FF = "\f"

PAGINATED = "\n".join(
    [
        "   The header contains the following fields, which are laid out in",
        "   the diagram below and described in the text that immediately",
        "",
        "      +--+--+--+--+",
        "      |    ID     |",
        "      +--+--+--+--+",
        "",
        "   Each field is an unsigned 16 bit integer, described in the text",
        "   that immediately",
        "Mockapetris                                                    [Page 26]",
        FF,
        "RFC 1035        Domain Implementation and Specification    November 1987",
        "",
        "   follows.  The ID is chosen by the requester.",
    ]
)


def test_page_furniture_goes_and_the_split_paragraph_rejoins() -> None:
    out = clean_section_text(PAGINATED)
    assert "[Page 26]" not in out
    assert "November 1987" not in out
    assert FF not in out
    # "…described in the text that immediately" + "follows." was one sentence
    # broken by the page break.
    assert "immediately follows." in out


def test_ascii_art_survives() -> None:
    out = clean_section_text(PAGINATED).split("\n")
    art = [line for line in out if "+--+" in line or "| " in line]
    assert art, "the diagram vanished"
    # Relative indentation is what makes a diagram a diagram: the common
    # three-column margin goes, the extra three do not.
    assert art[0].startswith("   +--+")


def test_dedent_removes_only_the_common_margin() -> None:
    out = clean_section_text("      alpha\n          beta\n")
    assert out == "alpha\n    beta"


def test_a_new_paragraph_after_a_break_is_not_glued_on() -> None:
    text = "\n".join(
        [
            "   This sentence ends here.",
            "Author                                          [Page 3]",
            FF,
            "RFC 9999   Something                              May 2026",
            "",
            "   A new paragraph begins.",
        ]
    )
    out = clean_section_text(text)
    assert "ends here.\nA new paragraph" in out


def test_a_slice_that_is_only_furniture_cleans_to_nothing() -> None:
    """RFC 635 has a chunk whose whole extent is a running header."""
    header = "RFC  635           An Assessment of ARPANET Protocols              May 1974"
    assert clean_section_text(header) == ""


def _chunk(rfc: str, section: Any, off: int, length: int, title: str = "T") -> ChunkMeta:
    return ChunkMeta(rfc=rfc, off=off, length=length, section=section, title=title)


def test_overlapping_chunks_become_one_span() -> None:
    # The chunker carries up to 500 chars forward, so consecutive chunks in a
    # section overlap; the union must not double-count.
    spans = section_spans(
        [
            _chunk("9111", "3", 100, 600),
            _chunk("9111", "3", 500, 600),
            _chunk("9111", "3", 900, 300),
        ]
    )
    assert len(spans) == 1
    assert (spans[0].start, spans[0].end) == (100, 1200)
    assert spans[0].length == 1100
    assert spans[0].chunks == 3


def test_unsectioned_chunks_stay_separate() -> None:
    """Without a heading, nothing claims two chunks are one passage."""
    spans = section_spans([_chunk("1", None, 0, 50), _chunk("1", None, 900, 50)])
    assert len(spans) == 2
    assert all(s.section is None for s in spans)


def test_spans_come_back_in_document_order() -> None:
    spans = section_spans(
        [
            _chunk("9111", "5", 900, 10),
            _chunk("9111", "2", 100, 10),
            _chunk("9110", "1", 700, 10),
        ]
    )
    assert [(s.rfc, s.section) for s in spans] == [
        ("9110", "1"),
        ("9111", "2"),
        ("9111", "5"),
    ]


def test_read_section_slices_the_mirror(tmp_path: Any) -> None:
    root = str(tmp_path)
    with open(os.path.join(root, "rfc9111.txt"), "wb") as fh:
        fh.write(b"PREAMBLE\n   the body of section three\n   continues here\nTAIL")
    # b"PREAMBLE\n" is 9 bytes; 46 more reaches the end of "here" without
    # picking up the trailing newline or TAIL.
    span = section_spans([_chunk("9111", "3", 9, 46)])[0]
    assert read_section(root, span) == "the body of section three\ncontinues here"


def test_absent_rfc_reads_empty_rather_than_raising(tmp_path: Any) -> None:
    span = section_spans([_chunk("9999", "1", 0, 10)])[0]
    assert read_section(str(tmp_path), span) == ""


def test_byte_offsets_are_bytes_not_characters(tmp_path: Any) -> None:
    """Offsets index the file, which is UTF-8; a multi-byte character before
    the span must not shift it."""
    root = str(tmp_path)
    body = "café\n   section text\n".encode("utf-8")
    with open(os.path.join(root, "rfc1.txt"), "wb") as fh:
        fh.write(body)
    start = body.index(b"   section")
    span = section_spans([_chunk("1", "1", start, len(b"   section text"))])[0]
    assert read_section(root, span) == "section text"


def test_chunk_counts_are_kept_for_reporting() -> None:
    spans = section_spans([_chunk("1", "1", 0, 10), _chunk("1", "1", 5, 10)])
    assert spans[0].chunks == 2


def _titles(spans: List[Any]) -> List[str]:
    return [s.title for s in spans]


def test_title_comes_from_the_section_s_chunks() -> None:
    spans = section_spans([_chunk("9110", "7.2", 10, 5, title="Host and :authority")])
    assert _titles(spans) == ["Host and :authority"]
