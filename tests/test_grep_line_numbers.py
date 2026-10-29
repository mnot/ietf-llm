"""grep_corpus line numbers agree with `read_file_section` and chunk spans.

Chunk spans and `read_file_section` count `\\n` only, but `str.splitlines`
also breaks on form feed, vertical tab, `\\x1c`-`\\x1e`, `\\x85` and U+2028/9.
Real drafts carry a form feed at every page break, so a grep over one reported
line numbers that drifted a line per page. The check that matters is the
round trip: the number grep prints, handed to `read_file_section`, must return
the line grep matched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Tuple

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings import chunk_spans
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.mcp.chunks import tool_read_file_section
from ietf_llm.mcp.grep import tool_grep_corpus
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.regexscan import excerpts, split_lines

from conftest import write_cache_file

_DRAFT = "drafts/draft-example-widget-00.txt"
_HIT_RE = re.compile(r"^ +(\d+): (.*?)(?:  \[chunk .*)?$")


def _page(number: int, body: List[str]) -> str:
    """One draft page in the shape the IETF text tools emit: body, footer,
    `\\n\\x0c\\n`, then the next page's running header."""
    return (
        "\n".join(body)
        + f"\n\nSmith                   Expires April 2027              [Page {number}]"
        + "\n\x0c\n"
        + "Internet-Draft              Widget Protocol              October 2026\n\n"
    )


def _real_shape_draft() -> str:
    pages = [
        ["Abstract", "", "   This document defines the widget NEEDLE protocol."],
        ["1.  Introduction", "", "   The NEEDLE appears on page two as well."],
        ["2.  Details", "", "   Nothing here.", "   A third NEEDLE, on page three."],
    ]
    out = "Internet-Draft              Widget Protocol              October 2026\n\n"
    out += "".join(_page(i, body) for i, body in enumerate(pages, 1))
    return out + "Author's Address\n\n   Final NEEDLE line\n"


def _hits(out: str) -> List[Tuple[int, str]]:
    """The `(lineno, text)` of every hit line in a rendered listing (context
    lines use `-`, not `:`, and are not hits)."""
    found = []
    for line in out.split("\n"):
        match = _HIT_RE.match(line)
        if match:
            found.append((int(match.group(1)), match.group(2)))
    return found


def _read_one(wg: str, file: str, lineno: int) -> str:
    out = tool_read_file_section(wg, file, start_line=lineno, max_lines=1)
    # `max_lines=1` is followed by a truncation marker line (and thread files
    # by a participation nudge); the requested line is the first.
    return out.split("\n")[0]


def _assert_round_trip(wg: str, file: str, out: str, needle: str) -> int:
    hits = _hits(out)
    for lineno, text in hits:
        assert needle.lower() in text.lower()
        assert _read_one(wg, file, lineno).strip() == text.strip()
    return len(hits)


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


# --- the helper ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", []),
        ("a\n", ["a"]),
        ("a\nb", ["a", "b"]),
        ("a\n\nb\n", ["a", "", "b"]),
        ("a\x0cb\nc\n", ["a\x0cb", "c"]),
        ("a\x0bb\x1cc\x85d e f\n", ["a\x0bb\x1cc\x85d e f"]),
    ],
)
def test_split_lines_breaks_on_newline_alone(text: str, expected: List[str]) -> None:
    assert split_lines(text) == expected


def test_split_lines_agrees_with_file_iteration(tmp_path: Path) -> None:
    text = _real_shape_draft()
    path = tmp_path / "d.txt"
    path.write_text(text, encoding="utf-8")
    with open(path, "r", encoding="utf-8") as fh:
        assert [ln.rstrip("\n") for ln in fh] == split_lines(text)


# --- grep_corpus --------------------------------------------------------------


def test_form_feed_draft_hits_round_trip_through_read_file_section(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    out = tool_grep_corpus("wg", "NEEDLE")
    assert "4 matching line(s) in 1 file(s)" in out
    assert _assert_round_trip("wg", _DRAFT, out, "NEEDLE") == 4


def test_form_feed_round_trip_in_regex_mode(isolated_home: Path) -> None:
    # The regex path runs in the child process; it must number the same way.
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    out = tool_grep_corpus("wg", r"\bNEEDLE\b", regex=True, case_sensitive=True)
    assert _assert_round_trip("wg", _DRAFT, out, "NEEDLE") == 4


def test_form_feed_round_trip_with_max_per_file(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    out = tool_grep_corpus("wg", "NEEDLE", max_per_file=3)
    assert _assert_round_trip("wg", _DRAFT, out, "NEEDLE") == 3


def test_form_feed_context_lines_are_the_neighbouring_lines(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    out = tool_grep_corpus("wg", "[Page 2]", context=2)
    checked = 0
    for line in out.split("\n"):
        match = re.match(r"^ +(\d+)([:-]) ?(.*)$", line)
        if match:
            shown = _read_one("wg", _DRAFT, int(match.group(1)))
            assert shown.rstrip() == match.group(3).rstrip()
            checked += 1
    # Two lines before, the hit, and two after: the form-feed line and the
    # running header are each one line, not split further.
    assert checked == 5


def test_form_feed_files_only_counts_match_the_listing(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    assert f"4 match(es)  {_DRAFT}" in tool_grep_corpus("wg", "NEEDLE", files_only=True)


def test_odd_separators_inside_a_line_do_not_split_it(isolated_home: Path) -> None:
    body = (
        "first\n"
        "before\x0bNEEDLE\x1cmid\x1d\x1e\x85after and more\n"
        "NEEDLE at the start of the line after\n"
        "last\n"
    )
    write_cache_file(isolated_home, "wg", "threads/odd.md", body)
    for regex in (False, True):
        out = tool_grep_corpus("wg", "NEEDLE", regex=regex)
        assert "2 matching line(s)" in out
        assert [n for n, _ in _hits(out)] == [2, 3]
        assert _assert_round_trip("wg", "threads/odd.md", out, "NEEDLE") == 2


@pytest.mark.parametrize("regex", [False, True])
def test_crlf_file_numbers_correctly_with_no_stray_cr(
    isolated_home: Path, regex: bool
) -> None:
    files = isolated_home / ".cache" / "ietf-llm" / "wg" / "files" / "threads"
    files.mkdir(parents=True)
    (files / "crlf.md").write_bytes(
        b"one\r\ntwo NEEDLE\r\n\r\nfour\r\nfive NEEDLE\r\n"
    )
    out = tool_grep_corpus("wg", "NEEDLE", regex=regex, context=1)
    assert "\r" not in out
    assert [n for n, _ in _hits(out)] == [2, 5]
    assert _read_one("wg", "threads/crlf.md", 5) == "five NEEDLE"
    assert "     1- one" in out
    assert "     4- four" in out


def test_no_phantom_line_after_the_final_newline(isolated_home: Path) -> None:
    # `x*` matches the empty string, so a spurious empty last "line" would be
    # counted as a hit that `read_file_section` cannot return.
    write_cache_file(isolated_home, "wg", "threads/short.md", "a\nb\n")
    out = tool_grep_corpus("wg", "x*", regex=True)
    assert "2 matching line(s)" in out


def test_hit_line_falls_inside_its_chunk_span(isolated_home: Path) -> None:
    """Grep's number must land in the chunk span the index recorded for the
    same text: both are `\\n` counts, so a form-feed draft puts each hit in
    the chunk that actually contains it."""
    write_cache_file(isolated_home, "wg", _DRAFT, _real_shape_draft())
    cache = get_wg_file_cache_dir("wg")
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index("wg", cache, model_name="stub", verbose=Verbosity.QUIET)
    spans = chunk_spans("wg", [_DRAFT]).get(_DRAFT)
    if not spans:
        pytest.skip("draft revision not chunked in this fixture")
    total = len(split_lines(_real_shape_draft()))
    assert max(end for _, _, end, _ in spans) <= total
    out = tool_grep_corpus("wg", "Final NEEDLE")
    [(lineno, _text)] = _hits(out)
    assert lineno == total
    assert any(start <= lineno <= end for _, start, end, _ in spans)


# --- section (RFC) excerpts ---------------------------------------------------


def test_excerpts_use_newline_only_offsets() -> None:
    text = "x\r\nneedle\nlast\n"
    matcher = re.compile("needle")
    assert excerpts(text, matcher, 0, 5) == [["needle"]]


def test_excerpts_context_is_not_split_at_a_form_feed() -> None:
    text = "intro\npage end\n\x0cInternet-Draft header\nneedle\ntail\n"
    matcher = re.compile("needle")
    assert excerpts(text, matcher, 1, 5) == [
        ["\x0cInternet-Draft header", "needle", "tail"]
    ]
