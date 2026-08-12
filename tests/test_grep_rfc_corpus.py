"""grep_corpus over the RFC series (`corpus="rfcs"`).

The series is not a gathered corpus — it has no `files/` dir, and the
plain-text mirror is publisher-side — so this backend scans the chunk table
instead. Two properties are what make it worth having, and neither is
exercised by the gathered-files tests:

  * a phrase is matched across a **line break**, because RFC text is
    hard-wrapped and almost every real sentence spans one; and
  * a phrase is matched across a **chunk boundary**, because rows carry the
    chunker's overlap trimmed off, so the joined section is the only place
    the text exists in full.

Both are the difference between answering "which RFCs contain this exact
sentence" and not, which is why they are asserted directly rather than through
a hit count.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from ietf_llm.embeddings.storage import _open_db, indexed_files, iter_sections
from ietf_llm.mcp import rfc_text
from ietf_llm.mcp.grep import tool_grep_corpus

#: (file, chunk_idx, title, text, section). Section 3 is deliberately split
#: mid-sentence across two chunks, and every row is wrapped, so the fixture
#: reproduces both boundaries a real RFC has.
ROWS = [
    ("rfc9111.txt", 0, "Introduction", "This document defines\ncaching.", "1"),
    ("rfc9111.txt", 1, "Storing Responses", "A cache MUST NOT store\na response", "3"),
    ("rfc9111.txt", 2, "Storing Responses", "unless the method\nis understood.", "3"),
    ("rfc7234.txt", 0, "Introduction", "An older caching\ndocument.", "1"),
]


@pytest.fixture(name="corpus")
def _corpus(isolated_home: Path) -> Path:
    conn = _open_db(rfc_text.RFC_CORPUS)
    try:
        vec = np.ones(4, dtype=np.float32).tobytes()
        conn.executemany(
            "INSERT INTO chunks(file, chunk_idx, sub_idx, title, text, embedding, "
            "section) VALUES(?,?,0,?,?,?,?)",
            [(f, i, t, x, vec, s) for f, i, t, x, s in ROWS],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
            [("model", "stub"), ("embed_dim", "4")],
        )
        conn.commit()
    finally:
        conn.close()
    d = isolated_home / ".cache" / "ietf-llm" / "_rfc"
    d.mkdir(parents=True, exist_ok=True)
    rfcs: Dict[str, Any] = {
        "RFC9111": {"title": "HTTP Caching", "obsoletes": ["RFC7234"]},
        "RFC7234": {"title": "HTTP/1.1 Caching"},
    }
    (d / "rfcs.json").write_text(json.dumps(rfcs), encoding="utf-8")
    (d / "refs.json").write_text("{}", encoding="utf-8")
    (d / "tags.json").write_text("{}", encoding="utf-8")
    return isolated_home


def test_iter_sections_joins_a_section_across_its_chunks(corpus: Path) -> None:
    assert corpus
    sections = {(f, s): text for f, s, _t, text in iter_sections(rfc_text.RFC_CORPUS)}
    assert sections[("rfc9111.txt", "3")] == (
        "A cache MUST NOT store\na response\nunless the method\nis understood."
    )
    assert indexed_files(rfc_text.RFC_CORPUS) == ["rfc7234.txt", "rfc9111.txt"]


def test_iter_sections_keeps_document_order(corpus: Path) -> None:
    assert corpus
    order = [s for f, s, _t, _x in iter_sections(rfc_text.RFC_CORPUS) if f == "rfc9111.txt"]
    assert order == ["1", "3"]


def test_phrase_matches_across_a_line_break(corpus: Path) -> None:
    """The wrap is where a line-oriented grep fails; this is the whole point."""
    assert corpus
    out = tool_grep_corpus("rfcs", "This document defines caching")
    assert "RFC 9111" in out
    assert "1 match(es)" in out


def test_phrase_matches_across_a_chunk_boundary(corpus: Path) -> None:
    """`a response unless the method` exists in neither row, only in the join."""
    assert corpus
    out = tool_grep_corpus("rfcs", "a response unless the method")
    assert "RFC 9111" in out
    assert "§3" in out


def test_hits_cite_rfc_and_section_not_line_numbers(corpus: Path) -> None:
    assert corpus
    out = tool_grep_corpus("rfcs", "MUST NOT store")
    assert "## RFC 9111" in out
    assert "§3 Storing Responses" in out
    assert 'get_rfc_section(rfc="<number>"' in out


def test_obsoleted_rfcs_are_marked(corpus: Path) -> None:
    """The series is the whole historical record, so a superseded hit must not
    be citable as current."""
    assert corpus
    out = tool_grep_corpus("rfcs", "older caching document")
    assert "RFC 7234" in out
    assert "obsoleted by" in out


def test_zero_reports_its_denominator_and_its_bound(corpus: Path) -> None:
    assert corpus
    out = tool_grep_corpus("rfcs", "quantum entanglement")
    assert "no matches" in out
    assert "scanned 2 RFC(s)" in out
    # A zero is only quotable if the caller knows what was outside the scan.
    assert "Front and back matter" in out


def test_file_pattern_narrows_and_says_so(corpus: Path) -> None:
    assert corpus
    out = tool_grep_corpus("rfcs", "caching", file_pattern="rfc9111.txt")
    assert "scanned 1 RFC(s)" in out
    assert "rfc9111.txt" in out


def test_file_pattern_matching_nothing_is_not_absence(corpus: Path) -> None:
    assert corpus
    out = tool_grep_corpus("rfcs", "caching", file_pattern="rfc42*.txt")
    assert "not** evidence of absence" in out


def test_files_only_gives_one_row_per_rfc(corpus: Path) -> None:
    assert corpus
    out = tool_grep_corpus("rfcs", "caching", files_only=True)
    assert "match(es)  RFC 9111" in out
    assert "match(es)  RFC 7234" in out


def test_regex_mode_is_not_whitespace_relaxed(corpus: Path) -> None:
    """A caller's own regex is passed through untouched — relaxing it would
    silently rewrite what they asked for."""
    assert corpus
    assert "no matches" in tool_grep_corpus("rfcs", r"defines caching", regex=True)
    assert "RFC 9111" in tool_grep_corpus("rfcs", r"defines\s+caching", regex=True)


def test_missing_corpus_says_how_to_install_it(isolated_home: Path) -> None:
    assert isolated_home
    out = tool_grep_corpus("rfcs", "anything")
    assert "not installed" in out
    assert "--init" in out


def test_a_failed_scan_never_renders_as_absence(corpus: Path, monkeypatch: Any) -> None:
    """The finding that mattered: `iter_sections` swallowing a sqlite3.Error
    ended the generator quietly, so a scan of a fraction of the corpus was
    rendered as a complete one — under a paragraph calling it real evidence of
    absence. An incomplete scan has no denominator and must say so."""
    assert corpus
    from ietf_llm.mcp import grep as grep_mod

    real = grep_mod.iter_sections

    def flaky(name: str, files: Any = None) -> Any:
        for index, row in enumerate(real(name, files)):
            if index >= 1:
                raise sqlite3.OperationalError("database is locked")
            yield row

    monkeypatch.setattr(grep_mod, "iter_sections", flaky)
    out = tool_grep_corpus("rfcs", "caching")
    assert "evidence of absence" not in out
    assert "Nothing can be concluded" in out


def test_an_unscoped_scan_names_no_files(corpus: Path, monkeypatch: Any) -> None:
    """An unscoped scan passed one bind parameter per RFC — 9,813 of them,
    past the 999-parameter default of SQLite before 3.32. The glob is the only
    thing that needs the IN clause."""
    assert corpus
    seen: Dict[str, Any] = {}
    from ietf_llm.mcp import grep as grep_mod

    real = grep_mod.iter_sections

    def spy(name: str, files: Any = None) -> Any:
        seen["files"] = files
        return real(name, files)

    monkeypatch.setattr(grep_mod, "iter_sections", spy)
    tool_grep_corpus("rfcs", "caching")
    assert seen["files"] is None
    tool_grep_corpus("rfcs", "caching", file_pattern="rfc9111.txt")
    assert seen["files"] == ["rfc9111.txt"]


def test_a_budgeted_stop_refuses_to_support_a_negative(
    corpus: Path, monkeypatch: Any
) -> None:
    assert corpus
    from ietf_llm.mcp import grep as grep_mod

    monkeypatch.setattr(grep_mod, "_RFC_SCAN_BUDGET_S", -1.0)
    monkeypatch.setattr(grep_mod, "_RFC_SCAN_CHECK_EVERY", 1)
    out = tool_grep_corpus("rfcs", "caching")
    assert "INCOMPLETE SCAN" in out
    assert "cannot support a negative claim" in out
    assert "evidence of absence" not in out


def test_the_listing_cap_reports_the_true_section_count(corpus: Path) -> None:
    """`len(hits)` is a retention artefact — capped overall and per RFC — so it
    cannot be the denominator in a sentence claiming the counts are complete."""
    assert corpus
    out = tool_grep_corpus("rfcs", "caching", limit=1)
    assert "Showing 1 of 2 matching section(s), at most 3 per RFC" in out
