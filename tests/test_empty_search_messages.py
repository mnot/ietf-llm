"""An empty index-backed read says why it is empty: the index is absent, a
filter excluded everything, or the query matched nothing. Only the first
suggests a re-gather. Nothing here reads the real cache.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings, mcp
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_stub(wg: str) -> None:
    cache = get_wg_file_cache_dir(wg)
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


def _seed(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-04-10-mlkem.md",
        "# MLKEM\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\nBody about MLKEM.\n\n"
        "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n\nA reply.\n",
    )


def test_search_empty_without_an_index_blames_the_index(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(isolated_home)  # corpus exists; no index built
    monkeypatch.setattr(mcp.search, "gather_enabled", lambda: True)
    out = mcp.search.tool_search("wg", "mlkem", author="Alice")
    assert "no results" in out
    assert "may have no search index" in out
    assert "start_gather" in out


def test_search_empty_with_a_filter_names_the_filters(
    isolated_home: Path,
) -> None:
    _seed(isolated_home)
    _build_with_stub("wg")
    out = mcp.search.tool_search(
        "wg", "mlkem", author="Zed", since="2026-01-01", role=None
    )
    assert "index is present" in out
    assert "author='Zed'" in out and "since='2026-01-01'" in out
    assert "role=" not in out  # unset filters are not listed
    assert 'read_digest(corpus="wg", kind="people")' in out
    assert "sender" in out
    assert "re-gather" not in out and "start_gather" not in out


def test_search_empty_filter_without_author_has_no_people_pointer(
    isolated_home: Path,
) -> None:
    _seed(isolated_home)
    _build_with_stub("wg")
    out = mcp.search.tool_search("wg", "mlkem", since="2030-01-01", sort="date")
    assert "since='2030-01-01'" in out and "sort='date'" in out
    assert "read_digest" not in out
    assert "re-gather" not in out


def test_search_empty_without_a_filter_does_not_suggest_a_regather(
    isolated_home: Path,
) -> None:
    # A valid index with no chunks in it: nothing can match, filter or not.
    write_cache_file(isolated_home, "wg", "notes/empty.txt", "")
    _build_with_stub("wg")
    out = mcp.search.tool_search("wg", "mlkem")
    assert "no results" in out
    assert "index is present" in out
    assert "no chunks matched" in out
    assert "Loosen" not in out
    assert "re-gather" not in out and "start_gather" not in out


def test_read_topic_empty_cases(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cache_file(isolated_home, "wg", "notes/empty.txt", "")
    monkeypatch.setattr(mcp.search, "gather_enabled", lambda: True)
    # Index absent.
    out = mcp.topic.tool_read_topic("wg", "mlkem")
    assert "may have no search index" in out
    # Index present, filter set.
    _build_with_stub("wg")
    out = mcp.topic.tool_read_topic("wg", "mlkem", since="2030-01-01")
    assert "index is present" in out and "since='2030-01-01'" in out
    assert "re-gather" not in out
    # Index present, no filter.
    out = mcp.topic.tool_read_topic("wg", "mlkem")
    assert "index is present" in out and "no chunks matched" in out
    assert "Loosen" not in out and "re-gather" not in out


def test_find_related_empty_cases(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(isolated_home)
    monkeypatch.setattr(mcp.search, "gather_enabled", lambda: True)
    f = "threads/2026-04-10-mlkem.md"
    out = mcp.search.tool_find_related("wg", f, 0)
    assert "no search index" in out and "start_gather" in out
    _build_with_stub("wg")
    out = mcp.search.tool_find_related("wg", f, 99)
    assert "index is present" in out and "no chunk 99" in out
    assert "re-gather" not in out and "start_gather" not in out
    out = mcp.search.tool_find_related("wg", f, 0, since="2030-01-01")
    assert "since='2030-01-01'" in out and "or the index has no chunk 0" in out
