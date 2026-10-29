"""The gather's topic-map step reads the index the gather just built.

On the cloud backend `_db_path_ro` resolves through the store to the current
*published* version's `embeddings.db`, so a re-gather's topic step would
cluster the previous corpus (and download that version to do it). The gather
wraps the step in `index_reads_from_workspace`; these tests drive the real
writer (`build_index` + `generate_topics`, stub embedder, hermetic cloud
store) rather than asserting against a hand-built sidecar.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings import storage
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import (
    _db_path,
    _db_path_ro,
    index_reads_from_workspace,
)
from ietf_llm.embeddings.topics import build_topics, generate_topics
from ietf_llm.gather import sequencer as main_mod
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore

from conftest import write_cache_file
from test_gather_runner import _stub_pipeline

_THEMES = {"quic": 0, "tls": 1, "http": 2, "dnssec": 3}


class _Stub:
    """Unit vector on the axis of the dominant theme keyword."""

    def embed(self, text: str) -> Iterable[float]:
        vec = [0.0] * 8
        vec[_THEMES[max(_THEMES, key=text.lower().count)]] = 1.0
        return vec

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _thread(theme: str, n: int) -> str:
    return (
        f"# {theme} item {n}\n\n**Span:** 2025-01-01 → 2025-03-01\n"
        "**Messages:** 1\n\n## Messages\n\n"
        f"### [1] 2025-03-01 10:00 — Alice\n\n" + f"{theme} " * 30 + "\n"
    )


def _seed_threads(home: Path, theme: str, count: int, start: int = 0) -> None:
    for n in range(start, start + count):
        write_cache_file(home, "wg", f"threads/2025-{theme}-{n}.md", _thread(theme, n))


def _index(wg: str = "wg") -> None:
    cache = get_wg_file_cache_dir(wg)
    assert build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


@pytest.fixture(autouse=True)
def _stub_embedder() -> Iterable[None]:
    embeddings._MODEL_CACHE["stub"] = _Stub()  # pylint: disable=protected-access
    yield
    embeddings._MODEL_CACHE.pop("stub", None)  # pylint: disable=protected-access


class _SpyStore(CloudCorpusStore):
    """A hermetic cloud store that counts reads of the published version."""

    staged = 0

    def _stage_current(self, corpus: str) -> Any:
        self.staged += 1
        return super()._stage_current(corpus)

    def local_index_dir(self, corpus: str) -> Any:
        self.staged += 1
        return super().local_index_dir(corpus)

    def local_corpus_dir(self, corpus: str) -> Any:
        self.staged += 1
        return super().local_corpus_dir(corpus)


@pytest.fixture
def cloud(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> _SpyStore:
    root = isolated_home / "cloud"
    store = _SpyStore(
        KvControlPlane(InMemoryKvStore()),
        FileBlobStore(str(root / "bucket")),
        str(root / "scratch"),
    )
    monkeypatch.setattr("ietf_llm.store.corpus.get_corpus_store", lambda: store)
    return store


def _n_docs_written() -> int:
    with open(Path(_db_path("wg")).parent / "topics.json", encoding="utf-8") as fh:
        return int(json.load(fh)["n_docs"])


def _publish_v1_then_grow(home: Path, store: _SpyStore) -> None:
    """Published v1 holds 12 documents; the workspace is then rebuilt with 16."""
    for theme in _THEMES:
        _seed_threads(home, theme, 3)
    _index()
    store.publish("wg", str(Path(get_wg_file_cache_dir("wg")).parent), version="v1")
    _seed_threads(home, "quic", 4, start=10)
    _index()
    store.staged = 0


def test_cloud_regather_topics_see_the_workspace_index(
    isolated_home: Path, cloud: _SpyStore
) -> None:
    _publish_v1_then_grow(isolated_home, cloud)
    with index_reads_from_workspace():
        assert _db_path_ro("wg") == _db_path("wg")
        assert generate_topics("wg", verbose=Verbosity.QUIET)

    assert _n_docs_written() == 16
    assert cloud.staged == 0, "the topic step must not stage the published version"
    # Control: outside the wrapper the same call reads the old published corpus,
    # so the assertions above would fail without the fix.
    payload = build_topics("wg")
    assert payload is not None and payload["n_docs"] == 12
    assert cloud.staged > 0


def test_first_gather_never_published_is_unchanged(
    isolated_home: Path, cloud: _SpyStore
) -> None:
    for theme in _THEMES:
        _seed_threads(isolated_home, theme, 3)
    _index()
    plain = build_topics("wg")
    with index_reads_from_workspace():
        wrapped = build_topics("wg")
        assert generate_topics("wg", verbose=Verbosity.QUIET)
    assert plain is not None and wrapped is not None
    assert wrapped["n_docs"] == plain["n_docs"] == 12
    assert _n_docs_written() == 12


def test_local_backend_is_unchanged(isolated_home: Path) -> None:
    for theme in _THEMES:
        _seed_threads(isolated_home, theme, 3)
    _index()
    assert _db_path_ro("wg") == _db_path("wg")
    plain = build_topics("wg")
    with index_reads_from_workspace():
        assert _db_path_ro("wg") == _db_path("wg")
        wrapped = build_topics("wg")
    assert plain is not None and wrapped is not None
    assert wrapped["n_docs"] == plain["n_docs"] == 12


# --- the context manager itself --------------------------------------------


def test_context_restores_state_on_exception() -> None:
    assert storage._READS_FROM_WORKSPACE.get() is False  # pylint: disable=protected-access
    with pytest.raises(RuntimeError):
        with index_reads_from_workspace():
            assert storage._READS_FROM_WORKSPACE.get() is True  # pylint: disable=protected-access
            raise RuntimeError("boom")
    assert storage._READS_FROM_WORKSPACE.get() is False  # pylint: disable=protected-access


def test_context_nests() -> None:
    flag = storage._READS_FROM_WORKSPACE  # pylint: disable=protected-access
    with index_reads_from_workspace():
        with index_reads_from_workspace():
            assert flag.get() is True
        assert flag.get() is True
    assert flag.get() is False


def test_context_does_not_leak_across_threads() -> None:
    flag = storage._READS_FROM_WORKSPACE  # pylint: disable=protected-access
    inside = threading.Event()
    release = threading.Event()
    seen: List[bool] = []

    def _reader() -> None:
        inside.wait(5)
        seen.append(flag.get())  # a concurrent served read on another thread
        release.set()

    worker = threading.Thread(target=_reader)
    worker.start()
    with index_reads_from_workspace():
        inside.set()
        assert release.wait(5)
        assert flag.get() is True
    worker.join()
    assert seen == [False]


def test_context_does_not_leak_across_tasks() -> None:
    flag = storage._READS_FROM_WORKSPACE  # pylint: disable=protected-access

    async def _scenario() -> List[bool]:
        entered = asyncio.Event()
        done = asyncio.Event()
        seen: List[bool] = []

        async def _gatherer() -> None:
            with index_reads_from_workspace():
                entered.set()
                await done.wait()

        async def _reader() -> None:
            await entered.wait()
            seen.append(flag.get())
            done.set()

        await asyncio.gather(_gatherer(), _reader())
        return seen

    assert asyncio.run(_scenario()) == [False]


# --- wiring ----------------------------------------------------------------


def test_gather_runs_topic_step_inside_the_wrapper(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_gather_one` (the CLI and the in-session gather child both reach it)
    calls `generate_topics` with the workspace-read flag set, and clears it."""
    _stub_pipeline(monkeypatch, (False, True))
    flag = storage._READS_FROM_WORKSPACE  # pylint: disable=protected-access
    observed: List[bool] = []
    monkeypatch.setattr(
        main_mod, "generate_topics", lambda *a, **k: observed.append(flag.get())
    )
    args = main_mod.build_parser().parse_args(["myorg"])
    main_mod._gather_one(args, Verbosity.QUIET)  # pylint: disable=protected-access
    assert observed == [True]
    assert flag.get() is False
