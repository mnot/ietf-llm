"""Tests for nearest-neighbour-by-example search (`related` /
`find_related`) and thread<->issue cross-surface bridging.

Like test_search_diversify.py, these need a stub that can *rank*, so they
share the same keyword presence-vector embedder: texts that share words
are genuinely more similar, and a text with no vocabulary word embeds to
the zero vector (cosine 0 — header chunks sink).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, related
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file

_VOCAB = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]


class _KeywordStubModel:
    """Presence-vector embedder over `_VOCAB`. Shared words → higher cosine."""

    def embed(self, text: str) -> Iterable[float]:  # llm.EmbeddingModel API
        low = text.lower()
        return [1.0 if word in low else 0.0 for word in _VOCAB]

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_keyword_stub(wg: str) -> None:
    embeddings._MODEL_CACHE["kw"] = _KeywordStubModel()  # pylint: disable=protected-access
    cache = get_wg_file_cache_dir(wg)
    build_index(wg, cache, model_name="kw", verbose=Verbosity.QUIET)


def _seed_thread(isolated_home: Path) -> None:
    """One thread: message [1] alpha/bravo, [2] alpha/bravo/charlie (close
    to [1]), [3] golf/hotel (unrelated)."""
    write_cache_file(
        isolated_home, "wg", "threads/2025-01-01-topic.md",
        (
            "# Topic\n\n## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\nalpha bravo body\n\n"
            "### [2] 2025-01-02 10:00 — Bob\n\nalpha bravo charlie body\n\n"
            "### [3] 2025-01-03 10:00 — Carol\n\ngolf hotel body\n"
        ),
    )


# --- related(): nearest-neighbour-by-example -------------------------------


def test_related_ranks_similar_above_dissimilar(isolated_home: Path) -> None:
    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    # Seed on message [1] (chunk_idx 1, "alpha bravo"). Pure relevance
    # (diversify off) so the ranking is the cosine order, not MMR's.
    hits = related(
        "wg", "threads/2025-01-01-topic.md", 1, k=10,
        diversify=False, verbose=Verbosity.QUIET,
    )
    titles = [h.title for h in hits]
    # The alpha/bravo/charlie message is nearer than the golf/hotel one.
    assert any("[2]" in t for t in titles)
    idx2 = next(i for i, t in enumerate(titles) if "[2]" in t)
    # [3] (golf/hotel) shares no words → cosine 0; if present it ranks
    # strictly below [2].
    idx3 = next((i for i, t in enumerate(titles) if "[3]" in t), len(titles))
    assert idx2 < idx3


def test_related_excludes_the_seed_chunk(isolated_home: Path) -> None:
    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    hits = related(
        "wg", "threads/2025-01-01-topic.md", 1, k=10,
        verbose=Verbosity.QUIET,
    )
    # The seed (file, chunk_idx) must never be its own result.
    assert not any(
        h.file == "threads/2025-01-01-topic.md" and h.chunk_idx == 1
        for h in hits
    )


def test_related_missing_chunk_returns_empty(isolated_home: Path) -> None:
    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    assert related(
        "wg", "threads/2025-01-01-topic.md", 999, verbose=Verbosity.QUIET,
    ) == []


def test_related_needs_no_embedding_backend(isolated_home: Path) -> None:
    # related() reads the seed's STORED vector — it never embeds a query.
    # Proof: drop the model from the cache after the index is built; a
    # query-string search would now fail to load, but related() still works.
    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    embeddings._MODEL_CACHE.pop("kw", None)  # pylint: disable=protected-access
    hits = related(
        "wg", "threads/2025-01-01-topic.md", 1, k=5,
        verbose=Verbosity.QUIET,
    )
    assert hits


# --- cross-surface bridging (thread <-> issue) -----------------------------


def test_related_cross_surface_thread_to_issue(isolated_home: Path) -> None:
    # The headline use: a thread message and the GitHub issue that captures
    # it sit close in the index. Seed on the thread, scope to issues/%, and
    # the matching issue comes back.
    _seed_thread(isolated_home)
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        (
            "# Issue #1: Alpha bravo question\n\n"
            "**Repository:** org/repo  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Dave _(opened issue)_\n\n"
            "alpha bravo delta in the issue\n"
        ),
    )
    _build_with_keyword_stub("wg")
    hits = related(
        "wg", "threads/2025-01-01-topic.md", 1, k=10,
        file_pattern="issues/%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.file.startswith("issues/") for h in hits)
    assert any(h.file == "issues/org-repo/1.md" for h in hits)


# --- tool_find_related rendering -------------------------------------------


def test_tool_find_related_renders_hits(isolated_home: Path) -> None:
    from ietf_llm import mcp_server

    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    out = mcp_server.tool_find_related("wg", "threads/2025-01-01-topic.md", 1, k=5)
    assert "score=" in out
    # Seed excluded; the related message [2] is rendered.
    assert "[2]" in out


def test_tool_find_related_missing_chunk_message(isolated_home: Path) -> None:
    from ietf_llm import mcp_server

    _seed_thread(isolated_home)
    _build_with_keyword_stub("wg")
    out = mcp_server.tool_find_related("wg", "threads/2025-01-01-topic.md", 999)
    assert "no related chunks" in out
