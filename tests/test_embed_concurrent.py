"""Concurrent embedding for the remote backend.

build_index overlaps the per-file embed round-trips through a bounded pool
when the model is the remote (`openai-embed/`) backend, while keeping every
SQLite write on the main thread. These guard the three things that matters:
the index is identical to the serial build (just produced concurrently),
the round-trips genuinely overlap, and a single file's embed failure is
isolated (the rest still index, the failer carries no hash stamp so it
retries). The on-device path stays serial — covered by the other embed tests.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import _db_path
from ietf_llm.utils import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

_REMOTE = "openai-embed/stub"  # the `openai-embed/` prefix selects the pool


class _SerialStub:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


class _ConcurrentStub:
    """Records peak in-flight calls so a test can prove the round-trips
    overlapped, and holds each call briefly so they actually do."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.calls = 0

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        with self._lock:
            self.inflight += 1
            self.calls += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(0.03)  # hold so concurrent calls overlap
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        finally:
            with self._lock:
                self.inflight -= 1


class _FlakyStub:
    """Raises for one file's content; embeds everything else."""

    def __init__(self, boom_text: str) -> None:
        self._boom = boom_text

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        if any(self._boom in t for t in texts):
            raise RuntimeError("simulated embed failure")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _chunk_rows(wg: str) -> List[tuple]:
    conn = sqlite3.connect(_db_path(wg))
    try:
        return sorted(
            conn.execute("SELECT file, chunk_idx, sub_idx FROM chunks").fetchall()
        )
    finally:
        conn.close()


def _seed_files(home: Path, wg: str, n: int) -> None:
    for i in range(n):
        write_cache_file(home, wg, f"drafts/draft-{i:02d}.txt", f"body of draft {i}\n")


def test_concurrent_index_matches_serial(
    isolated_home: Path, monkeypatch
) -> None:
    # Same corpus, two backends: the concurrent remote build must produce the
    # identical chunk set as the serial on-device build.
    monkeypatch.setenv("IETF_LLM_EMBED_CONCURRENCY", "4")
    embeddings._MODEL_CACHE["stub"] = _SerialStub()  # noqa: SLF001
    embeddings._MODEL_CACHE[_REMOTE] = _ConcurrentStub()  # noqa: SLF001

    _seed_files(isolated_home, "serialwg", 6)
    _seed_files(isolated_home, "concwg", 6)

    n_serial = build_index(
        "serialwg", get_wg_file_cache_dir("serialwg"), "stub", verbose=Verbosity.QUIET
    )
    n_conc = build_index(
        "concwg", get_wg_file_cache_dir("concwg"), _REMOTE, verbose=Verbosity.QUIET
    )

    assert n_conc == n_serial > 0
    # Same relpaths in both, so the (file, chunk_idx, sub_idx) sets match.
    assert [r[1:] for r in _chunk_rows("concwg")] == [
        r[1:] for r in _chunk_rows("serialwg")
    ]


def test_concurrent_path_actually_overlaps(
    isolated_home: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IETF_LLM_EMBED_CONCURRENCY", "4")
    stub = _ConcurrentStub()
    embeddings._MODEL_CACHE[_REMOTE] = stub  # noqa: SLF001
    _seed_files(isolated_home, "wg", 6)

    build_index("wg", get_wg_file_cache_dir("wg"), _REMOTE, verbose=Verbosity.QUIET)

    assert stub.calls == 6  # one embed_multi per file
    assert stub.max_inflight > 1  # the round-trips overlapped


def test_concurrency_one_is_serial(isolated_home: Path, monkeypatch) -> None:
    # Floor of 1 forces serial even on the remote backend (no overlap).
    monkeypatch.setenv("IETF_LLM_EMBED_CONCURRENCY", "1")
    stub = _ConcurrentStub()
    embeddings._MODEL_CACHE[_REMOTE] = stub  # noqa: SLF001
    _seed_files(isolated_home, "wg", 4)

    build_index("wg", get_wg_file_cache_dir("wg"), _REMOTE, verbose=Verbosity.QUIET)

    assert stub.calls == 4
    assert stub.max_inflight == 1


def test_concurrent_one_file_failure_isolated(
    isolated_home: Path, monkeypatch
) -> None:
    # One file's embed raises; the others still index, and the failed file
    # carries no hash stamp, so a clean re-build picks it up.
    monkeypatch.setenv("IETF_LLM_EMBED_CONCURRENCY", "4")
    _seed_files(isolated_home, "wg", 5)
    cache = get_wg_file_cache_dir("wg")

    embeddings._MODEL_CACHE[_REMOTE] = _FlakyStub("draft 2")  # noqa: SLF001
    build_index("wg", cache, _REMOTE, verbose=Verbosity.QUIET)
    files = {r[0] for r in _chunk_rows("wg")}
    assert "drafts/draft-02.txt" not in files  # the failer is absent
    assert len(files) == 4  # the other four indexed

    # A healthy model on the next build embeds the previously-failed file
    # without re-touching the four already stamped.
    embeddings._MODEL_CACHE[_REMOTE] = _SerialStub()  # noqa: SLF001
    n = build_index("wg", cache, _REMOTE, verbose=Verbosity.QUIET)
    assert n > 0  # the retried file embedded
    assert "drafts/draft-02.txt" in {r[0] for r in _chunk_rows("wg")}


def test_remote_reports_percent_to_detail(
    isolated_home: Path, monkeypatch
) -> None:
    # The remote (pool) path feeds the same byte-weighted % to the stage
    # `detail` callback gather_status surfaces. Force the throttle open so every
    # completed file emits.
    import importlib

    search_module = importlib.import_module("ietf_llm.embeddings.search")
    monkeypatch.setattr(search_module, "_PROGRESS_SECS", 0)
    embeddings._MODEL_CACHE[_REMOTE] = _SerialStub()  # noqa: SLF001
    _seed_files(isolated_home, "wg", 3)
    seen: List[str] = []
    build_index(
        "wg",
        get_wg_file_cache_dir("wg"),
        _REMOTE,
        verbose=Verbosity.QUIET,
        detail=seen.append,
    )
    assert seen and all(s.endswith("%") for s in seen)
