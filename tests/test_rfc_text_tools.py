"""The RFC full-text tools (#230).

Built against a small hand-made corpus in the schema the importer writes, so
what is exercised is the read path — section assembly, label normalisation,
the outline-on-miss behaviour, and the obsoletion marker that stops a
superseded RFC being cited as current.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from ietf_llm.embeddings.storage import _db_path, _open_db, section_outline, section_rows
from ietf_llm.mcp import rfc_text
from ietf_llm.mcp.rfc_text import (
    _normalise_section,
    section_text,
    tool_get_rfc_section,
)

#: (file, chunk_idx, title, text, section)
ROWS = [
    ("rfc9111.txt", 0, "Introduction", "This document defines caching.", "1"),
    ("rfc9111.txt", 1, "Storing Responses", "A cache MUST NOT store a response", "3"),
    ("rfc9111.txt", 2, "Storing Responses", "unless the method is understood.", "3"),
    ("rfc9111.txt", 3, "Storing Incomplete", "An incomplete response MAY be", "3.3"),
    ("rfc9111.txt", 4, "Constructing", "When presented with a request,", "4"),
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
    return isolated_home


def _seed_rfc_metadata(home: Path, obsoleted: bool = False) -> None:
    """A minimal `_rfc/rfcs.json`, which is what supplies titles and status."""
    d = home / ".cache" / "ietf-llm" / "_rfc"
    d.mkdir(parents=True, exist_ok=True)
    rfcs: Dict[str, Any] = {
        "RFC9111": {"title": "HTTP Caching", "status": "current"},
        "RFC7234": {"title": "HTTP/1.1 Caching", "obsoletes": []},
    }
    if obsoleted:
        rfcs["RFC9111"] = {"title": "HTTP Caching", "obsoletes": ["RFC7234"]}
    (d / "rfcs.json").write_text(json.dumps(rfcs), encoding="utf-8")
    (d / "refs.json").write_text("{}", encoding="utf-8")
    (d / "tags.json").write_text("{}", encoding="utf-8")


# --- section label normalisation -------------------------------------------


@pytest.mark.parametrize(
    "asked,expected",
    [
        ("7.2", "7.2"),
        ("§7.2", "7.2"),
        ("§ 7.2", "7.2"),
        ("Section 7.2", "7.2"),
        ("section 7.2", "7.2"),
        ("sec. 7.2", "7.2"),
        ("7.2.", "7.2"),
        ("  7.2  ", "7.2"),
        ("Appendix A", "A"),
        ("appendix a.1", "A.1"),
        ("a", "A"),
    ],
)
def test_labels_are_normalised_as_cited(asked: str, expected: str) -> None:
    assert _normalise_section(asked) == expected


def test_nonsense_label_is_rejected() -> None:
    assert _normalise_section("the caching bit") is None


# --- section assembly -------------------------------------------------------


def test_a_section_is_assembled_from_its_rows_in_order(corpus: Path) -> None:
    """Rows hold overlap-trimmed text; only the whole run is faithful."""
    assert section_text("9111", "3") == (
        "A cache MUST NOT store a response\nunless the method is understood."
    )


def test_section_rows_are_scoped_to_their_file_and_label(corpus: Path) -> None:
    assert len(section_rows(rfc_text.RFC_CORPUS, "rfc9111.txt", "3")) == 2
    assert section_rows(rfc_text.RFC_CORPUS, "rfc9111.txt", "9") == []


def test_outline_is_document_order_not_lexical(corpus: Path) -> None:
    outline = section_outline(rfc_text.RFC_CORPUS, "rfc9111.txt")
    assert [s for s, _t, _n in outline] == ["1", "3", "3.3", "4"]


# --- the tools --------------------------------------------------------------


def test_parent_label_returns_its_subsections(corpus: Path) -> None:
    _seed_rfc_metadata(corpus)
    out = tool_get_rfc_section("9111", "3")
    assert "3.3" in out
    assert "An incomplete response MAY be" in out


def test_exact_label_does_not_pull_in_a_sibling(corpus: Path) -> None:
    _seed_rfc_metadata(corpus)
    out = tool_get_rfc_section("9111", "4")
    assert "When presented with a request" in out
    assert "incomplete response" not in out


def test_a_miss_returns_the_outline(corpus: Path) -> None:
    """A citation may be real but unindexed (references, front matter) or
    simply wrong; showing what exists answers both."""
    _seed_rfc_metadata(corpus)
    out = tool_get_rfc_section("9111", "19")
    assert "no indexed section 19" in out
    assert "| 3.3 |" in out
    assert "not indexed" in out


def test_no_section_returns_the_outline(corpus: Path) -> None:
    _seed_rfc_metadata(corpus)
    out = tool_get_rfc_section("9111")
    assert "indexed sections" in out
    assert "| 1 | Introduction |" in out


def test_an_obsoleted_rfc_is_marked(corpus: Path) -> None:
    """The citation guard: superseded text stays available, never silent."""
    _seed_rfc_metadata(corpus, obsoleted=True)
    conn = sqlite3.connect(_db_path(rfc_text.RFC_CORPUS))
    try:
        vec = np.ones(4, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT INTO chunks(file, chunk_idx, sub_idx, title, text, embedding, "
            "section) VALUES('rfc7234.txt', 0, 0, 'Storing', 'old text', ?, '3')",
            (vec,),
        )
        conn.commit()
    finally:
        conn.close()
    assert "obsoleted by RFC9111" in tool_get_rfc_section("7234", "3")
    assert "obsoleted by" not in tool_get_rfc_section("9111", "3")


def test_an_uninstalled_corpus_says_so_rather_than_returning_nothing(
    isolated_home: Path,
) -> None:
    out = tool_get_rfc_section("9111", "3")
    assert "not installed" in out


def test_the_read_path_does_not_import_the_publisher_package() -> None:
    """`ietf_llm.rfcindex` fetches releases and rsyncs mirrors — it reaches the
    network, and the MCP surface must stay offline. The two share meta-key
    constants, which is exactly the kind of import that reintroduces the
    dependency, so it is checked rather than trusted.

    In a subprocess because module caching makes an in-process check depend on
    whatever the rest of the suite imported first.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, ietf_llm.mcp.rfc_text as m; "
            "print(any(k.startswith('ietf_llm.rfcindex') for k in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "False", proc.stdout
