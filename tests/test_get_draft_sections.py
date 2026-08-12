"""get_draft: outline, section reads, and honest truncation.

The bug behind these: the tool returned a 2000-line window by default, which
is ~88,000 characters — past an MCP client's per-result limit, so the client
truncated it *again*, silently, cutting off the "continue with start_line=N"
footer that would have said so. What survived was a `#` header line that reads
as part of the draft. A caller could review half a document and never know.

So what is asserted here is mostly about the *bound* and the *banner*: that a
result stays small enough that our truncation is the only one, that a partial
read says so at both ends in a voice that is not the document's, and that a
complete section is never stamped partial merely because the document goes on.

Section addressing is the real fix — it is what makes truncation rare rather
than merely announced — and it is reader-side: headings are parsed from the
cached text on each call, so no re-gather is involved.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.mcp.drafts import (
    _MAX_WINDOW_CHARS,
    _draft_headings,
    _section_span,
    tool_get_draft,
)

from conftest import write_cache_file

#: Shaped like xml2rfc's text output: body indented, headings at column 0, a
#: dot-leadered table of contents that must NOT be mistaken for headings, and
#: a numbered list item that must not either.
DRAFT = """\
HTTP                                                          M. Example
Internet-Draft                                              Example Corp
Intended status: Standards Track                        1 February 2026


                          A Test Draft

Table of Contents

   1.  Introduction  . . . . . . . . . . . . . . . . . . . . . . . .   2
   4.  Behaviour . . . . . . . . . . . . . . . . . . . . . . . . . .   3
     4.1.  Client  . . . . . . . . . . . . . . . . . . . . . . . . .   3

1.  Introduction

   This document is a fixture.  It has prose that mentions Section 4.1
   without being a heading, and a list:

   1.  first item, indented, not a heading
   2.  second item

4.  Behaviour

   Behaviour in general.

4.1.  Client

   What a client does.

4.2.  Server

   What a server does.

Appendix A.  Rationale

   Why.
"""


def _seed(home: Path) -> None:
    write_cache_file(home, "wg", "drafts/draft-ietf-wg-test-03.txt", DRAFT)


def test_headings_exclude_the_toc_and_list_items(isolated_home: Path) -> None:
    labels = [label for label, _t, _l in _draft_headings(DRAFT.splitlines(True))]
    assert labels == ["1", "4", "4.1", "4.2", "A"]


def test_parent_section_takes_its_descendants(isolated_home: Path) -> None:
    lines = DRAFT.splitlines(True)
    headings = _draft_headings(lines)
    start, end = _section_span(headings, "4", len(lines))
    body = "".join(lines[start - 1 : end])
    assert "Behaviour in general" in body
    assert "What a client does" in body
    assert "What a server does" in body
    # ... and stops before the next non-descendant.
    assert "Why." not in body


def test_default_call_returns_the_outline(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_get_draft("draft-ietf-wg-test")
    assert "| Section | Title |" in out
    assert "| 4.1 | Client |" in out
    assert "| A | Rationale |" in out
    # The outline is the cheap thing: it must not carry the document body.
    assert "What a client does" not in out


def test_section_read_returns_that_section_only(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_get_draft("draft-ietf-wg-test", section="4.1")
    assert "What a client does" in out
    assert "What a server does" not in out
    assert "This document is a fixture" not in out


def test_cited_section_forms_are_accepted(isolated_home: Path) -> None:
    _seed(isolated_home)
    for asked in ("4.1", "§4.1", "Section 4.1", "4.1."):
        assert "What a client does" in tool_get_draft("draft-ietf-wg-test", section=asked)
    assert "Why." in tool_get_draft("draft-ietf-wg-test", section="Appendix A")


def test_a_complete_section_is_not_stamped_partial(isolated_home: Path) -> None:
    """The document continuing past the section is not truncation."""
    _seed(isolated_home)
    out = tool_get_draft("draft-ietf-wg-test", section="4.1")
    assert "PARTIAL READ" not in out


def test_missing_section_returns_the_outline(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_get_draft("draft-ietf-wg-test", section="99")
    assert "no section 99" in out
    assert "| Section | Title |" in out


def test_truncation_is_banner_stamped_at_both_ends(isolated_home: Path) -> None:
    big = "".join(f"1.  Section {i}\n\n" + ("   filler line\n" * 400) for i in range(30))
    write_cache_file(isolated_home, "wg", "drafts/draft-ietf-wg-big-00.txt", big)
    out = tool_get_draft("draft-ietf-wg-big", start_line=1)
    assert out.startswith("**PARTIAL READ")
    assert out.rstrip().endswith("`.")
    assert out.count("PARTIAL READ") == 2
    assert "continue with `start_line=" in out.lower()


def test_a_read_stays_under_the_character_bound(isolated_home: Path) -> None:
    """The bound that stops a client truncating on top of us."""
    big = "".join(f"1.  Section {i}\n\n" + ("   filler line\n" * 400) for i in range(30))
    write_cache_file(isolated_home, "wg", "drafts/draft-ietf-wg-big-00.txt", big)
    out = tool_get_draft("draft-ietf-wg-big", start_line=1)
    assert len(out) < _MAX_WINDOW_CHARS + 2000  # body bound plus the banners


def test_draft_without_headings_falls_back_to_reading_from_the_top(
    isolated_home: Path,
) -> None:
    write_cache_file(
        isolated_home, "wg", "drafts/draft-ietf-wg-old-00.txt", "   just prose\n" * 20
    )
    out = tool_get_draft("draft-ietf-wg-old")
    assert "No parseable section headings" in out
    assert "just prose" in out


def test_uncached_draft_still_says_so(isolated_home: Path) -> None:
    assert isolated_home
    assert "No cached draft" in tool_get_draft("draft-ietf-wg-absent")
