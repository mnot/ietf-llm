"""Concurrency / partial-write hardening for the index serving path.

Covers: search() closes its connection on every return path (no fd / WAL
leak under a long-lived server); build_index checkpoints the WAL so the
published .db is a self-contained object; the per-corpus build lock; the
IETF_LLM_INDEX_IMMUTABLE read mode; and the readiness probe opening a
real index instead of just stat-ing the dir.

Uses a stub embedding model (constant vector) so it's fast and offline.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List

import importlib

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.embeddings.storage import _db_building_path, _db_path
from ietf_llm.mcp_server import _readiness
from ietf_llm.utils import Verbosity, get_index_dir, get_wg_file_cache_dir

from conftest import write_cache_file

# The package __init__ re-exports `search` (the function), shadowing the
# submodule attribute -- so fetch the module object via importlib to patch
# the names search() resolved at import (`_connect_ro`, `file_lock`).
search_mod = importlib.import_module("ietf_llm.embeddings.search")


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed_model() -> None:
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access


def _build(wg: str, isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, wg, "threads/2025-01-01-topic.md",
        "# Topic\n\n## Messages\n\n### [1] 2025-01-01 10:00 — Alice\n\nbody\n",
    )
    _seed_model()
    build_index(wg, get_wg_file_cache_dir(wg), model_name="stub", verbose=Verbosity.QUIET)


class _ConnProxy:
    """Forwards the two methods search() uses, recording close()."""

    def __init__(self, conn: object) -> None:
        self._c = conn
        self.closed = False

    def cursor(self):  # type: ignore[no-untyped-def]
        return self._c.cursor()

    def close(self) -> None:
        self.closed = True
        self._c.close()


def _spy_connections(monkeypatch) -> list:  # type: ignore[no-untyped-def]
    proxies: list = []
    real = search_mod._connect_ro  # pylint: disable=protected-access

    def spy(wg: str):  # type: ignore[no-untyped-def]
        p = _ConnProxy(real(wg))
        proxies.append(p)
        return p

    monkeypatch.setattr(search_mod, "_connect_ro", spy)
    return proxies


# --- build_index: self-contained DB + per-corpus lock ----------------------


def test_build_index_leaves_no_wal_sidecar(isolated_home: Path) -> None:
    _build("wg", isolated_home)
    db = _db_path("wg")
    assert os.path.exists(db)
    # Checkpoint(TRUNCATE) + close fold the WAL back in, so the published
    # index is a single self-contained object with no -wal dependency.
    assert not os.path.exists(db + "-wal")


def _write_second(wg: str, isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, wg, "threads/2025-02-02-two.md",
        "# Two\n\n## Messages\n\n### [1] 2025-02-02 10:00 — Bob\n\nsecond body\n",
    )


def test_failed_build_leaves_live_index_intact(isolated_home: Path, monkeypatch) -> None:
    # A build writes a scratch copy and only swaps it in at the end, so a build
    # that dies partway must leave the live index exactly as it was -- a reader
    # keeps seeing the previous complete snapshot, never a half-built one.
    _build("wg", isolated_home)
    before = {h.file for h in search("wg", "q", k=50, verbose=Verbosity.QUIET)}
    _write_second("wg", isolated_home)  # cache now has a second file to index
    _seed_model()

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("build blew up mid-embed")

    monkeypatch.setattr(search_mod, "_build_index_locked", boom)
    try:
        build_index(
            "wg", get_wg_file_cache_dir("wg"), model_name="stub", verbose=Verbosity.QUIET
        )
    except RuntimeError:
        pass
    after = {h.file for h in search("wg", "q", k=50, verbose=Verbosity.QUIET)}
    assert after == before  # unchanged: the new file never made it in
    assert not os.path.exists(_db_building_path("wg"))  # scratch discarded


def test_successful_build_promotes_and_clears_scratch(isolated_home: Path) -> None:
    _build("wg", isolated_home)
    _write_second("wg", isolated_home)
    _seed_model()
    build_index(
        "wg", get_wg_file_cache_dir("wg"), model_name="stub", verbose=Verbosity.QUIET
    )
    files = {h.file for h in search("wg", "q", k=50, verbose=Verbosity.QUIET)}
    assert any("two" in f for f in files)  # new content visible after the swap
    assert not os.path.exists(_db_building_path("wg"))  # no scratch left behind


def test_build_index_takes_per_corpus_lock(isolated_home: Path, monkeypatch) -> None:
    calls: list = []
    real = search_mod.file_lock

    @contextmanager
    def spy(path: str):  # type: ignore[no-untyped-def]
        calls.append(path)
        with real(path):
            yield

    monkeypatch.setattr(search_mod, "file_lock", spy)
    _build("wg", isolated_home)
    assert calls == [_db_path("wg") + ".lock"]


# --- search(): no connection leak on early returns -------------------------


def test_search_closes_connection_on_empty_result(isolated_home: Path, monkeypatch) -> None:
    _build("wg", isolated_home)
    proxies = _spy_connections(monkeypatch)
    # A label filter matches no thread chunk (labels are NULL there), so the
    # query takes the `if not rows: return []` path -- which must still close.
    hits = search("wg", "q", k=5, label="no-such-label", verbose=Verbosity.QUIET)
    assert hits == []
    assert proxies and all(p.closed for p in proxies)


def test_search_closes_connection_when_model_unavailable(
    isolated_home: Path, monkeypatch
) -> None:
    _build("wg", isolated_home)
    # Evict the stub so _get_embed_model returns None -> the `model is None`
    # early return; it must close the connection it already opened.
    embeddings._MODEL_CACHE.pop("stub", None)  # pylint: disable=protected-access
    proxies = _spy_connections(monkeypatch)
    hits = search("wg", "q", verbose=Verbosity.QUIET)
    assert hits == []
    assert proxies and all(p.closed for p in proxies)


# --- immutable read mode ---------------------------------------------------


def test_immutable_read_returns_hits(isolated_home: Path, monkeypatch) -> None:
    _build("wg", isolated_home)
    monkeypatch.setenv("IETF_LLM_INDEX_IMMUTABLE", "1")
    _seed_model()  # query-time embed needs the stub too
    hits = search("wg", "anything", k=5, verbose=Verbosity.QUIET)
    # immutable=1 reads the main DB file directly; this only sees the chunks
    # if build_index checkpointed the WAL into it (which it does).
    assert hits


# --- readiness probe opens a real index ------------------------------------


def test_readiness_no_corpora_is_ready(isolated_home: Path) -> None:
    os.makedirs(get_index_dir(), exist_ok=True)
    ready, detail = _readiness()
    assert ready
    assert detail["index_probe"] == "no-corpora"


def test_readiness_ok_with_index(isolated_home: Path) -> None:
    _build("wg", isolated_home)
    ready, detail = _readiness()
    assert ready
    assert detail["index_probe"] == "ok"


def test_readiness_failed_on_unreadable_index(isolated_home: Path) -> None:
    # A present-but-corrupt embeddings.db must fail the probe (and readiness)
    # rather than false-green on a bare directory stat.
    d = os.path.join(get_index_dir(), "wg")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "embeddings.db"), "w", encoding="utf-8") as fh:
        fh.write("not a sqlite database")
    ready, detail = _readiness()
    assert not ready
    assert detail["index_probe"] == "failed"
