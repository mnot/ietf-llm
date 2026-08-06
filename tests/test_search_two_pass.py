"""Ranking must hold nothing that scales with the corpus.

`_rank` scans the whole candidate set to score it, so anything it keeps per
row is paid for once per chunk in the corpus — and an index of the whole
RFC series is ~380k chunks. Three bounds keep a query proportional to k
instead: the scan selects only id / collapse key / vector, it scores each
batch and drops the vectors, and the two things ranking genuinely needs
back — embeddings for the MMR pool, display columns for the survivors —
are fetched by id afterwards.

These lock all three in.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import (
    _MMR_POOL,
    _SCAN_BATCH,
    _scan_candidates,
    build_index,
    search,
)
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

_VOCAB = ["alpha", "bravo", "charlie", "delta"]


class _KeywordStubModel:
    def embed(self, text: str) -> Iterable[float]:
        low = text.lower()
        return [1.0 if word in low else 0.0 for word in _VOCAB]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed_and_build(isolated_home: Path, wg: str = "wg") -> None:
    for n in range(12):
        body = f"# T{n}\n\n## Messages\n\n"
        for i in (1, 2, 3):
            body += f"### [{i}] 2025-01-0{i} 10:00 — P{n}\n\nalpha bravo msg {n}\n\n"
        write_cache_file(isolated_home, wg, f"threads/t{n:02d}-topic.md", body)
    embeddings._MODEL_CACHE["kw"] = _KeywordStubModel()  # pylint: disable=protected-access
    build_index(wg, get_wg_file_cache_dir(wg), model_name="kw", verbose=Verbosity.QUIET)


def _traced_search(monkeypatch, wg: str, **kwargs) -> List[str]:
    """Run a search, returning every SELECT issued against `chunks`."""
    import sqlite3  # pylint: disable=import-outside-toplevel

    seen: List[str] = []
    real_connect = sqlite3.connect

    def _connect(*args, **kw):
        conn = real_connect(*args, **kw)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _connect)
    search(wg, "alpha", verbose=Verbosity.QUIET, model_name="kw", **kwargs)
    return [s for s in seen if "FROM chunks" in s]


def test_scan_does_not_select_text(isolated_home, monkeypatch):
    _seed_and_build(isolated_home)
    statements = _traced_search(monkeypatch, "wg", k=3)
    scans = [s for s in statements if " WHERE id IN " not in s]
    assert scans, "expected a full scan over chunks"
    for scan in scans:
        columns = re.search(r"SELECT (.+?) FROM chunks", scan, re.S)
        assert columns, scan
        selected = {c.strip() for c in columns.group(1).split(",")}
        # Ranking needs the vector and the collapse key, nothing wider.
        assert selected == {"id", "file", "chunk_idx", "embedding"}, selected


def test_display_fetch_is_bounded_by_k(isolated_home, monkeypatch):
    _seed_and_build(isolated_home)
    statements = _traced_search(monkeypatch, "wg", k=3)
    fetches = [s for s in statements if " WHERE id IN " in s and "text" in s]
    assert len(fetches) == 1, statements
    # One bind placeholder per selected row -- never per candidate.
    assert fetches[0].count("?") <= 3


def test_vector_fetch_is_bounded_by_the_mmr_pool(isolated_home, monkeypatch):
    # Diversification needs embeddings back, but only for the pool it
    # actually compares -- not for every candidate it scored.
    _seed_and_build(isolated_home)
    statements = _traced_search(monkeypatch, "wg", k=3)
    fetches = [s for s in statements if " WHERE id IN " in s and "embedding" in s]
    assert len(fetches) == 1, statements
    assert fetches[0].count("?") <= _MMR_POOL


def test_scan_is_streamed_not_materialised():
    # The scan must never pull the whole candidate set into memory: it
    # scores batch by batch and lets each batch go.
    import numpy as np  # pylint: disable=import-outside-toplevel

    dim = len(_VOCAB)
    rows = [
        (i, f"threads/t{i}.md", 0, np.ones(dim, dtype=np.float32).tobytes())
        for i in range(_SCAN_BATCH * 2 + 7)
    ]

    class _Cursor:
        def __init__(self):
            self.pos = 0
            self.batches = 0

        def fetchmany(self, size):
            self.batches += 1
            out = rows[self.pos : self.pos + size]
            self.pos += len(out)
            return out

        def fetchall(self):  # pragma: no cover - must never be reached
            raise AssertionError("scan must stream, not fetchall")

    cur = _Cursor()
    cand = _scan_candidates(cur, np.ones(dim, dtype=np.float32))
    assert len(cand.ids) == len(rows)
    assert len(cand.scores) == len(rows)
    assert cand.keys[0] == ("threads/t0.md", 0)
    # Three full batches plus the empty read that ends the loop.
    assert cur.batches == 4
