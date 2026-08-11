"""The bge query-side retrieval instruction (#230).

Two things are worth pinning down. Which model ids take the instruction —
an over-broad match would put an English instruction on a Chinese model, and
a too-narrow one silently loses the recall it buys. And that it reaches the
model on the *query* path and only there: it is query-side by construction,
so a passage that got it would be embedded wrongly and stay that way in the
index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.models import (
    DEFAULT_EMBED_MODEL,
    _BGE_EN_QUERY_PREFIX,
    query_prefix,
)
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


def test_default_model_takes_the_instruction() -> None:
    assert query_prefix(DEFAULT_EMBED_MODEL) == _BGE_EN_QUERY_PREFIX


def test_remote_bge_takes_it_too() -> None:
    # Same weights reached through an OpenAI-compatible endpoint.
    assert query_prefix("openai-embed/@cf/baai/bge-small-en-v1.5")
    assert query_prefix("openai-embed/BAAI/bge-large-en-v1.5")


@pytest.mark.parametrize(
    "model",
    [
        "stub",
        "sentence-transformers/all-MiniLM-L6-v2",
        # A different instruction, not this one.
        "BAAI/bge-small-zh-v1.5",
        "openai-embed/text-embedding-3-small",
    ],
)
def test_other_models_get_no_instruction(model: str) -> None:
    assert query_prefix(model) == ""


def test_env_override_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_QUERY_PREFIX", "off")
    assert query_prefix(DEFAULT_EMBED_MODEL) == ""
    monkeypatch.setenv("IETF_LLM_QUERY_PREFIX", "ON")
    assert query_prefix(DEFAULT_EMBED_MODEL) == _BGE_EN_QUERY_PREFIX


class _RecordingModel:
    """Scores every chunk equally and remembers what it was asked to embed."""

    def __init__(self) -> None:
        self.seen: List[str] = []

    def embed(self, text: str) -> Iterable[float]:
        self.seen.append(text)
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed(home: Path) -> None:
    write_cache_file(
        home,
        "wg",
        "threads/2026-01-01-caching.md",
        "# Caching\n\n## Messages\n\n"
        "### [1] 2026-01-01 09:00 — Alice\n\nA response may be stored.\n",
    )


def _index_under(model_id: str) -> _RecordingModel:
    model = _RecordingModel()
    # pylint: disable-next=protected-access
    embeddings._MODEL_CACHE[model_id] = model
    build_index("wg", get_wg_file_cache_dir("wg"), model_name=model_id, verbose=Verbosity.QUIET)
    model.seen.clear()  # drop the passage-side embeds from the build
    return model


def test_query_is_prefixed_for_a_bge_index(isolated_home: Path) -> None:
    _seed(isolated_home)
    model = _index_under("sentence-transformers/BAAI/bge-small-en-v1.5")
    search("wg", "when may a response be stored", k=1)
    assert model.seen == [_BGE_EN_QUERY_PREFIX + "when may a response be stored"]


def test_query_is_untouched_for_other_models(isolated_home: Path) -> None:
    _seed(isolated_home)
    model = _index_under("stub")
    search("wg", "when may a response be stored", k=1)
    assert model.seen == ["when may a response be stored"]


def test_passages_are_never_prefixed(isolated_home: Path) -> None:
    """The instruction is query-side; a prefixed passage would be embedded
    wrongly and persist that way in the index."""
    _seed(isolated_home)
    model = _RecordingModel()
    # pylint: disable-next=protected-access
    embeddings._MODEL_CACHE["sentence-transformers/BAAI/bge-small-en-v1.5"] = model
    build_index(
        "wg",
        get_wg_file_cache_dir("wg"),
        model_name="sentence-transformers/BAAI/bge-small-en-v1.5",
        verbose=Verbosity.QUIET,
    )
    assert model.seen, "the build embedded nothing"
    assert not any(t.startswith(_BGE_EN_QUERY_PREFIX) for t in model.seen)
