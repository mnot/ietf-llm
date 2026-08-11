"""Assembling an embeddings.db from the published index (#230).

The load-bearing claim is that a section is exactly the concatenation of its
rows: the chunker overlaps consecutive chunks, so rows store only the part no
earlier chunk covered. If that trimming is wrong the corpus gains duplicated
prose and every section read repeats a paragraph.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ietf_llm.embeddings.storage import ENCODING_INT8, read_codec
from ietf_llm.log import Verbosity
from ietf_llm.rfcindex import format as fmt
from ietf_llm.rfcindex.build import (
    CHUNKER_ID,
    META_SOURCE_BUILD,
    META_SOURCE_COMMIT,
    META_SOURCE_MODEL,
    QUERY_MODEL,
    build_rfc_index,
)

DIMS = 4
BODY = (
    b"HEADER LINE\n"
    b"   Alpha paragraph about caching responses in a store.\n"
    b"\n"
    b"   Beta paragraph explaining when a cache must not store.\n"
    b"\n"
    b"   Gamma paragraph on revalidation of stale entries.\n"
)


def _cluster(rows: List[Dict[str, Any]], ident: int) -> bytes:
    strs: List[str] = []

    def intern(v: str) -> int:
        if v not in strs:
            strs.append(v)
        return strs.index(v)

    tail: Dict[str, Any] = {
        "n": len(rows),
        "rfc": [r["rfc"] for r in rows],
        "off": [r["off"] for r in rows],
        "len": [r["len"] for r in rows],
        "sec": [intern(r["sec"]) if r["sec"] else -1 for r in rows],
        "title": [intern(r["title"]) for r in rows],
    }
    tail["str"] = strs
    body = json.dumps(tail).encode("utf-8")
    vecs = np.arange(len(rows) * DIMS, dtype=np.int8).tobytes()
    head = struct.pack(
        "<4sHHIIII", fmt.MAGIC_CLUSTER, 1, DIMS, len(rows), ident, len(body), 0
    )
    return head + vecs + body


def _index(
    root: str, rows: List[Dict[str, Any]], digests: Optional[Dict[str, str]] = None
) -> str:
    os.makedirs(os.path.join(root, fmt.CLUSTERS_DIR), exist_ok=True)
    manifest = {
        "version": 1,
        "build": "20260811T003915Z",
        "built": "2026-08-11T00:39:15Z",
        "source": {"commit": "d640e524"},
        "model": {"id": "Xenova/bge-small-en-v1.5", "dims": DIMS},
        "quant": {"scale": 0.25},
        "chunks": {"count": len(rows)},
        "clusters": {"count": 1},
    }
    with open(os.path.join(root, fmt.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    with open(os.path.join(root, fmt.CENTROIDS_NAME), "wb") as fh:
        fh.write(
            struct.pack("<4sHHIIII", fmt.MAGIC_CENTROIDS, 1, DIMS, 1, 0, 0, 0)
            + bytes(DIMS)
        )
    with open(fmt.cluster_path(root, 0), "wb") as fh:
        fh.write(_cluster(rows, 0))
    if digests is not None:
        with open(os.path.join(root, fmt.SOURCES_NAME), "w", encoding="utf-8") as fh:
            json.dump({"digest": "sha256", "rfcs": digests}, fh)
    return root


def _mirror(root: str, rfc: str = "9111", body: bytes = BODY) -> str:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, f"rfc{rfc}.txt"), "wb") as fh:
        fh.write(body)
    return root


#: Two overlapping chunks of section "3": the second starts inside the first,
#: which is what the chunker's carried-forward paragraph looks like.
OVERLAPPING = [
    {"rfc": "9111", "off": 12, "len": 110, "sec": "3", "title": "Storing Responses"},
    {"rfc": "9111", "off": 70, "len": 100, "sec": "3", "title": "Storing Responses"},
]


def _rows(db: str) -> List[Tuple[Any, ...]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT file, chunk_idx, title, text, section, url, embedding "
            "FROM chunks ORDER BY chunk_idx"
        ).fetchall()
    finally:
        conn.close()


def _build(tmp_path: Any, rows: List[Dict[str, Any]], **kw: Any) -> str:
    index = _index(str(tmp_path / "index"), rows, kw.pop("digests", None))
    mirror = _mirror(str(tmp_path / "mirror"), **kw)
    db = str(tmp_path / "out.db")
    build_rfc_index(index, mirror, db, verbosity=Verbosity.QUIET)
    return db


def test_overlapping_chunks_store_disjoint_text(tmp_path: Any) -> None:
    rows = _rows(_build(tmp_path, OVERLAPPING))
    assert len(rows) == 2
    first, second = rows[0][3], rows[1][3]
    # No sentence appears in both rows: the overlap was trimmed at write time.
    assert "Beta paragraph" not in first or "Beta paragraph" not in second
    joined = "\n".join(r[3] for r in rows)
    assert joined.count("Beta paragraph") == 1


def test_a_section_is_the_concatenation_of_its_rows(tmp_path: Any) -> None:
    rows = _rows(_build(tmp_path, OVERLAPPING))
    joined = " ".join(" ".join(r[3].split()) for r in rows)
    assert "Alpha paragraph" in joined
    assert "Beta paragraph" in joined
    assert "Gamma paragraph" in joined


def test_rows_carry_section_file_and_url(tmp_path: Any) -> None:
    file, _idx, title, _text, section, url, _vec = _rows(_build(tmp_path, OVERLAPPING))[0]
    assert file == "rfc9111.txt"
    assert section == "3"
    assert title == "Storing Responses"
    assert url == "https://www.rfc-editor.org/rfc/rfc9111.txt"


def test_vectors_are_stored_int8_with_the_manifest_scale(tmp_path: Any) -> None:
    db = _build(tmp_path, OVERLAPPING)
    conn = sqlite3.connect(db)
    try:
        codec = read_codec(conn)
        blob = conn.execute("SELECT embedding FROM chunks LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert codec.encoding == ENCODING_INT8
    assert codec.scale == 0.25
    assert len(blob) == DIMS  # one byte per dimension, not four


def test_meta_names_the_query_model_and_keeps_provenance(tmp_path: Any) -> None:
    """`model` has to name something our loader can build, since `search`
    constructs an embedder from it; the producing id is kept beside it."""
    db = _build(tmp_path, OVERLAPPING)
    conn = sqlite3.connect(db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    assert meta["model"] == QUERY_MODEL
    assert meta["chunker_version"] == CHUNKER_ID
    assert meta[META_SOURCE_MODEL] == "Xenova/bge-small-en-v1.5"
    assert meta[META_SOURCE_BUILD] == "20260811T003915Z"
    assert meta[META_SOURCE_COMMIT] == "d640e524"


def test_a_reissued_rfc_is_dropped_not_mis_joined(tmp_path: Any) -> None:
    """The digest names bytes we do not have, so its offsets describe text we
    cannot read; a partial corpus beats a mis-attributed one."""
    db = _build(tmp_path, OVERLAPPING, digests={"9111": "0" * 64})
    assert _rows(db) == []


def test_a_matching_digest_keeps_the_rfc(tmp_path: Any) -> None:
    import hashlib

    db = _build(
        tmp_path, OVERLAPPING, digests={"9111": hashlib.sha256(BODY).hexdigest()}
    )
    assert len(_rows(db)) == 2


def test_chunks_that_clean_to_nothing_are_dropped(tmp_path: Any) -> None:
    """RFC 635 has a chunk whose entire extent is a running header."""
    header = b"RFC 9111   HTTP Caching                     June 2022"
    tail = b"   Real body here."
    body = header + b"\n" + tail + b"\n"
    rows = [
        # The whole extent is the running header, so it cleans to nothing.
        {"rfc": "9111", "off": 0, "len": len(header), "sec": "1", "title": "Header"},
        {
            "rfc": "9111",
            "off": len(header) + 1,
            "len": len(tail),
            "sec": "2",
            "title": "Real",
        },
    ]
    out = _rows(_build(tmp_path, rows, body=body))
    assert [r[4] for r in out] == ["2"]


def test_chunk_idx_is_document_order(tmp_path: Any) -> None:
    rows = [
        {"rfc": "9111", "off": 120, "len": 50, "sec": "5", "title": "Later"},
        {"rfc": "9111", "off": 12, "len": 50, "sec": "1", "title": "Earlier"},
    ]
    out = _rows(_build(tmp_path, rows))
    assert [r[1] for r in out] == [0, 1]
    assert [r[4] for r in out] == ["1", "5"]


def test_an_rfc_absent_from_the_mirror_is_skipped(tmp_path: Any) -> None:
    rows = list(OVERLAPPING) + [
        {"rfc": "9999", "off": 0, "len": 20, "sec": "1", "title": "Missing"}
    ]
    out = _rows(_build(tmp_path, rows))
    assert {r[0] for r in out} == {"rfc9111.txt"}


def test_a_differing_digest_is_reported_as_skipped(tmp_path: Any) -> None:
    """The stat, not just the corpus. An earlier version computed
    `usable - grouped`, which is the RFCs that matched and had no chunks —
    the opposite of what the field means — so a reissued RFC was correctly
    dropped from the corpus and silently absent from the report.
    """
    index = _index(str(tmp_path / "index"), OVERLAPPING, {"9111": "0" * 64})
    mirror = _mirror(str(tmp_path / "mirror"))
    db = str(tmp_path / "out.db")
    stats = build_rfc_index(index, mirror, db, verbosity=Verbosity.QUIET)
    assert stats.skipped_rfcs == ["9111"]
    assert "1 RFCs skipped" in stats.summary()
    assert _rows(db) == []


def test_a_matching_digest_reports_nothing_skipped(tmp_path: Any) -> None:
    import hashlib

    index = _index(
        str(tmp_path / "index"),
        OVERLAPPING,
        {"9111": hashlib.sha256(BODY).hexdigest()},
    )
    mirror = _mirror(str(tmp_path / "mirror"))
    stats = build_rfc_index(
        index, mirror, str(tmp_path / "out.db"), verbosity=Verbosity.QUIET
    )
    assert stats.skipped_rfcs == []
    assert "skipped" not in stats.summary()


def test_rows_never_split_a_source_line(tmp_path: Any) -> None:
    """Chunk boundaries land mid-line, and rows are joined with newlines when
    a section is reassembled — so a boundary inside a line splits it. Found in
    RFC 1531's DHCP state machine, which came back with `    |` on one line
    and `+--------+  DHCPACK/ …` on the next, breaking the figure.
    """
    body = (
        b"HEADER\n"
        b"    |   +--------+     DHCPACK/       |          |\n"
        b"    |              Record lease, set  |          |\n"
        b"    |                timers T1, T2    |          |\n"
    )
    # Two chunks whose boundary falls in the middle of the second line.
    rows = [
        {"rfc": "1531", "off": 7, "len": 20, "sec": "4", "title": "Art"},
        {"rfc": "1531", "off": 20, "len": 90, "sec": "4", "title": "Art"},
    ]
    out = _rows(_build(tmp_path, rows, rfc="1531", body=body))
    joined = "\n".join(r[3] for r in out)
    source_lines = [l.strip() for l in body.decode().split("\n") if l.strip()][1:]
    for line in source_lines:
        assert line in " ".join(joined.split("\n")) or line in joined, (
            f"{line!r} was split across rows"
        )
    # And the diagram row survives whole rather than as two fragments.
    assert "+--------+     DHCPACK/       |          |" in joined
