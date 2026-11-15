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

import pytest
import requests

from ietf_llm import embeddings
from ietf_llm.embeddings.models import _OpenAICompatEmbeddingModel
from ietf_llm.embeddings.oai_compat import RETRY_AFTER_MAX, UpstreamRateLimited
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import _db_path
from ietf_llm.log import Verbosity
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
    assert seen and all("%" in s and "embedded" in s for s in seen)


class _Resp:
    def __init__(self, status: int, n: int = 1, headers=None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._n = n

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return {
            "data": [
                {"index": i, "embedding": [1.0, 0.0, 0.0, 0.0]} for i in range(self._n)
            ]
        }


def test_over_cap_retry_after_stops_the_build(
    isolated_home: Path, monkeypatch
) -> None:
    # Several windows in flight against the real remote client; the first
    # file request is refused with an hour-long Retry-After. No request may
    # follow it once the client knows, the build stops with the cause named,
    # and files that did embed are kept (unfinished ones retry next gather).
    monkeypatch.setenv("IETF_LLM_EMBED_CONCURRENCY", "2")
    lock = threading.Lock()
    posts: List[str] = []
    state = {"limited": False, "ok": 0}

    def fake_post(self, url, headers, json, timeout):  # noqa: ARG001
        texts = json["input"]
        if texts == ["dimension probe"]:
            return _Resp(200)
        with lock:
            posts.append(texts[0])
            first = not state["limited"]
            state["limited"] = True
        if first:
            return _Resp(429, headers={"Retry-After": "3600"})
        time.sleep(0.1)
        with lock:
            state["ok"] += 1
        return _Resp(200, len(texts))

    monkeypatch.setattr(requests.Session, "post", fake_post)
    model = _OpenAICompatEmbeddingModel(
        "stub", "https://host/v1", {}, batch_size=1, timeout=5.0, max_retries=3
    )
    embeddings._MODEL_CACHE[_REMOTE] = model  # noqa: SLF001
    _seed_files(isolated_home, "limitedwg", 12)

    with pytest.raises(UpstreamRateLimited) as exc:
        build_index(
            "limitedwg",
            get_wg_file_cache_dir("limitedwg"),
            _REMOTE,
            verbose=Verbosity.QUIET,
        )

    msg = str(exc.value)
    assert "rate limited" in msg and "3600s" in msg and "next gather" in msg
    assert exc.value.retry_after == 3600
    # Only the requests already in flight (the pool has two workers) went out;
    # the other windows never reached the provider.
    assert len(posts) <= 2
    kept = _chunk_rows("limitedwg")
    assert len(kept) == state["ok"] < 12
    assert f"{len(kept)} files embedded and kept" in msg


def test_model_refuses_requests_while_blocked(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_post(self, url, headers, json, timeout):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, headers={"Retry-After": "3600"})
        return _Resp(200)

    monkeypatch.setattr(requests.Session, "post", fake_post)
    model = _OpenAICompatEmbeddingModel(
        "m", "https://host/v1", {}, batch_size=1, timeout=5.0, max_retries=3
    )
    with pytest.raises(UpstreamRateLimited):
        model.embed_multi(["a"])
    # Blocked: later calls, from any path, fail without a request ...
    with pytest.raises(UpstreamRateLimited):
        model.embed("b")
    with pytest.raises(UpstreamRateLimited):
        model.embed_multi(["c"])
    assert calls["n"] == 1
    # ... for at most RETRY_AFTER_MAX, however long the header said.
    assert model._blocked_until - time.monotonic() <= RETRY_AFTER_MAX
    model._blocked_until = 0.0
    assert model.embed("d") == [1.0, 0.0, 0.0, 0.0]
