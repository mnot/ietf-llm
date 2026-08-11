"""int8 vector storage (#230).

An index imported from already-quantised vectors keeps them quantised;
dequantising on import would inflate 167 MiB to 670 MiB and recover no
precision, since it is already gone. What matters is that the encoding
travels with the index and that a float32 index is completely unaffected.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List

import numpy as np
import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.embeddings.storage import (
    ENCODING_FLOAT32,
    ENCODING_INT8,
    FLOAT32_CODEC,
    VectorCodec,
    _connect_ro,
    _db_path,
    _pack,
    _unpack_matrix,
    pack_vector,
    read_codec,
    write_codec,
)
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

SCALE = 0.0035588767115525373


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def test_absent_meta_reads_as_float32() -> None:
    """Every index written before this existed has no such key."""
    assert read_codec(_mem()) == FLOAT32_CODEC


def test_float32_writes_no_meta_at_all() -> None:
    """A float32 index's meta stays byte-identical to one written before."""
    conn = _mem()
    write_codec(conn, FLOAT32_CODEC)
    assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == 0


def test_int8_codec_round_trips_through_meta() -> None:
    conn = _mem()
    write_codec(conn, VectorCodec(ENCODING_INT8, SCALE))
    codec = read_codec(conn)
    assert codec.encoding == ENCODING_INT8
    assert codec.scale == SCALE
    assert codec.itemsize == 1


def test_int8_without_a_scale_is_refused() -> None:
    """Scaling by a silent 1.0 would read as a catastrophic quality
    regression rather than as a broken index."""
    conn = _mem()
    conn.execute("INSERT INTO meta VALUES('vector_encoding', 'int8')")
    with pytest.raises(ValueError, match="no positive vector_scale"):
        read_codec(conn)


def test_unpack_dequantises_without_renormalising() -> None:
    codec = VectorCodec(ENCODING_INT8, 0.5)
    blob = np.array([2, 0, 0, 4], dtype=np.int8).tobytes()
    out = _unpack_matrix([blob], codec)
    assert out.dtype == np.float32
    # 2*0.5 and 4*0.5 — a renormalising reader would return unit rows.
    assert out.tolist() == [[1.0, 0.0, 0.0, 2.0]]


def test_unpack_defaults_to_float32_behaviour() -> None:
    vec = [0.6, 0.8, 0.0, 0.0]
    assert np.allclose(_unpack_matrix([_pack(vec)]), [vec])


def test_int8_dimension_is_derived_from_the_codec() -> None:
    """Same byte count means a different dimension under each encoding."""
    blob = bytes(16)
    assert _unpack_matrix([blob], VectorCodec(ENCODING_INT8, 0.5)).shape == (1, 16)
    assert _unpack_matrix([blob], FLOAT32_CODEC).shape == (1, 4)


def test_pack_int8_survives_a_round_trip() -> None:
    codec = VectorCodec(ENCODING_INT8, SCALE)
    vec = np.random.RandomState(0).randn(384).astype(np.float32)
    vec /= np.linalg.norm(vec)
    blob = pack_vector(vec, codec)
    assert len(blob) == 384
    back = _unpack_matrix([blob], codec)[0]
    # Quantisation is lossy, but only slightly: rfc.fyi's manifest reports a
    # mean cosine of 0.9998 for the same scale over the real corpus.
    cosine = float(back @ vec / np.linalg.norm(back))
    assert cosine > 0.999


def test_pack_clips_rather_than_wrapping() -> None:
    """A component beyond the quantisation range must saturate; wrapping
    would flip a strong signal to its opposite."""
    codec = VectorCodec(ENCODING_INT8, 0.001)
    blob = pack_vector([1.0, 0.0, 0.0, 0.0], codec)
    assert np.frombuffer(blob, dtype=np.int8).tolist() == [127, 0, 0, 0]


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def test_a_normal_gather_still_writes_float32(isolated_home: Path) -> None:
    """The regression that matters: nothing about an ordinary corpus changes."""
    write_cache_file(
        isolated_home,
        "wg",
        "threads/2026-01-01-t.md",
        "# T\n\n## Messages\n\n### [1] 2026-01-01 09:00 — A\n\nA response may be stored.\n",
    )
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index("wg", get_wg_file_cache_dir("wg"), model_name="stub", verbose=Verbosity.QUIET)

    conn = _connect_ro("wg")
    try:
        assert read_codec(conn) == FLOAT32_CODEC
        blob = conn.execute("SELECT embedding FROM chunks LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert len(blob) == 8 * 4  # eight float32 dimensions, as before
    assert _db_path("wg")
    assert search("wg", "stored response", k=1)


def test_search_reads_an_int8_index_end_to_end(isolated_home: Path) -> None:
    """Convert a built index to int8 in place and confirm search still ranks."""
    write_cache_file(
        isolated_home,
        "wg",
        "threads/2026-01-01-t.md",
        "# T\n\n## Messages\n\n### [1] 2026-01-01 09:00 — A\n\nA response may be stored.\n",
    )
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index("wg", get_wg_file_cache_dir("wg"), model_name="stub", verbose=Verbosity.QUIET)

    codec = VectorCodec(ENCODING_INT8, 0.01)
    conn = sqlite3.connect(_db_path("wg"))
    try:
        rows = conn.execute("SELECT id, embedding FROM chunks").fetchall()
        for row_id, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            conn.execute(
                "UPDATE chunks SET embedding=? WHERE id=?",
                (pack_vector(vec, codec), row_id),
            )
        write_codec(conn, codec)
        conn.commit()
    finally:
        conn.close()

    hits = search("wg", "stored response", k=1)
    assert hits, "an int8 index returned nothing"
    assert hits[0].score > 0.9
