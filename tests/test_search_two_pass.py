"""Ranking must not read chunk `text` for every candidate.

`_rank` scans the whole candidate set to score it, so anything it selects
is paid for once per chunk in the corpus. `text` is the widest column in
the table; selecting it during the scan makes a query allocate in
proportion to the corpus rather than to k, which an index of the whole RFC
series would not survive. The display columns are fetched in a second pass
for the selected rows only.

These lock that in: the scan's column list, and the bound that the second
pass touches at most k rows.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
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


def test_second_pass_is_bounded_by_k(isolated_home, monkeypatch):
    _seed_and_build(isolated_home)
    statements = _traced_search(monkeypatch, "wg", k=3)
    fetches = [s for s in statements if " WHERE id IN " in s]
    assert len(fetches) == 1, statements
    assert "text" in fetches[0]
    # One bind placeholder per selected row -- never per candidate.
    assert fetches[0].count("?") <= 3
