"""Tests for cross-corpus semantic search (`tool_search_corpora`).

Uses the same stub-embedding-model approach as test_search_filters: the
stub returns a constant vector, so every chunk scores 1.0. That can't
exercise *ranking* (all scores tie), but it's enough to assert the
cross-corpus plumbing — which corpora contribute, how hits are tagged,
how the model-id grouping renders, and how skips/caps are reported.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.utils import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file
from test_search_filters import _StubModel  # noqa: F401


def _build(wg: str, model_name: str = "stub") -> None:
    """Build an embedding index for an already-seeded corpus under a named
    stub model, so the index records `model_name` in its meta."""
    embeddings._MODEL_CACHE[model_name] = _StubModel()  # pylint: disable=protected-access
    build_index(wg, get_wg_file_cache_dir(wg), model_name=model_name, verbose=Verbosity.QUIET)


def _seed_thread(isolated_home: Path, wg: str, slug: str, body: str) -> None:
    write_cache_file(
        isolated_home, wg, f"threads/2026-01-01-{slug}.md",
        (
            f"# {slug}\n\n## Messages\n\n"
            "### [1] 2026-01-01 10:00 — Alice\n\n"
            f"{body}\n"
        ),
    )


def test_empty_corpora_returns_guidance() -> None:
    out = mcp.search.tool_search_corpora([], "anything")
    assert "find_efforts" in out
    assert "list_corpora" in out


def test_merges_hits_tagged_by_corpus(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha discusses the topic")
    _seed_thread(isolated_home, "beta", "topic-b", "beta discusses the topic")
    _build("alpha")
    _build("beta")
    out = mcp.search.tool_search_corpora(["alpha", "beta"], "topic")
    # Hits from both corpora, each tagged with its origin.
    assert "corpus=alpha" in out
    assert "corpus=beta" in out
    # Same model → one comparable ranking.
    assert "directly comparable" in out


def test_k_bounds_total_hits(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha body")
    _seed_thread(isolated_home, "beta", "topic-b", "beta body")
    _build("alpha")
    _build("beta")
    out = mcp.search.tool_search_corpora(["alpha", "beta"], "topic", k=1)
    # k caps the merged total, not per-corpus.
    assert "[1]" in out
    assert "[2]" not in out


def test_unknown_corpus_reported_not_silent(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha body")
    _build("alpha")
    out = mcp.search.tool_search_corpora(["alpha", "nope"], "topic")
    assert "corpus=alpha" in out
    assert "Skipped" in out
    assert "nope" in out


def test_no_index_corpus_reported(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha body")
    _build("alpha")
    # gamma has cached files but was never embedded.
    _seed_thread(isolated_home, "gamma", "topic-g", "gamma body")
    out = mcp.search.tool_search_corpora(["alpha", "gamma"], "topic")
    assert "corpus=alpha" in out
    assert "no embedding index" in out
    assert "gamma" in out


def test_mixed_models_grouped_and_interleaved(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha body")
    _seed_thread(isolated_home, "beta", "topic-b", "beta body")
    _build("alpha", model_name="model-a")
    _build("beta", model_name="model-b")
    out = mcp.search.tool_search_corpora(["alpha", "beta"], "topic")
    # Different models → not merged on raw score; grouped + interleaved.
    assert "different embedding models" in out
    assert "`model-a`" in out
    assert "`model-b`" in out
    assert "corpus=alpha" in out
    assert "corpus=beta" in out


def test_no_results_still_reports_skips(isolated_home: Path) -> None:
    # All requested corpora are unknown → a no-results body that still
    # names why each was skipped.
    out = mcp.search.tool_search_corpora(["nope1", "nope2"], "topic")
    assert "no results" in out
    assert "nope1" in out
    assert "nope2" in out


def test_caps_fanout_and_reports_drop() -> None:
    # 13 names exceeds the 12-corpus cap; the overflow is reported, not
    # silently dropped. (All unknown here — the point is the cap note.)
    names = [f"c{i}" for i in range(13)]
    out = mcp.search.tool_search_corpora(names, "topic")
    assert "cap" in out
    assert "c12" in out


def test_dedups_corpus_names(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "alpha", "topic-a", "alpha body")
    _build("alpha")
    out = mcp.search.tool_search_corpora(["alpha", "alpha", " alpha "], "topic")
    # De-duped to a single corpus; renders once in the comparable ranking.
    assert out.count("Ranked across 1 corpora") == 1
