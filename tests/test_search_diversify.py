"""Tests for MMR diversification of search results.

Unlike test_search_filters.py — whose stub returns one constant vector,
so cosine is 1.0 between everything and the model can't *rank* — these
need a stub that discriminates. `_KeywordStubModel` embeds each text as a
presence vector over a small fixed vocabulary, so two messages that share
words are genuinely more similar than two that don't, and a text with no
vocabulary word embeds to the zero vector (cosine 0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.utils import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

# One basis dimension per word. Deliberately excludes any "fallback"
# collision: a text with no vocabulary word embeds to all-zeros, so
# thread/issue *header* chunks (which carry no body words) score 0.
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


def _seed_two_equal_relevance_clusters(isolated_home: Path) -> None:
    """Two threads of EQUAL relevance to the query 'alpha' but distinct
    from each other: A's messages are alpha/bravo, B's are alpha/charlie.
    cos('alpha', either) = 0.707; cos(A-vec, B-vec) = 0.5. So plain top-k
    fills with whichever file is iterated first; MMR should reach across."""
    body_a = "# A\n\n## Messages\n\n"
    for i in (1, 2, 3):
        body_a += f"### [{i}] 2025-01-0{i} 10:00 — P\n\nalpha bravo msg\n\n"
    write_cache_file(isolated_home, "wg", "threads/aaa-topic.md", body_a)
    body_b = "# B\n\n## Messages\n\n"
    for i in (1, 2, 3):
        body_b += f"### [{i}] 2025-01-0{i} 10:00 — Q\n\nalpha charlie msg\n\n"
    write_cache_file(isolated_home, "wg", "threads/bbb-topic.md", body_b)


def test_diversify_off_concentrates_on_one_cluster(isolated_home: Path) -> None:
    _seed_two_equal_relevance_clusters(isolated_home)
    _build_with_keyword_stub("wg")
    hits = search(
        "wg", "alpha", k=3, diversify=False, verbose=Verbosity.QUIET,
    )
    files = {h.file for h in hits}
    # Plain relevance top-k: equal scores, so it takes the first cluster
    # iterated (aaa- sorts before bbb-) and never reaches the other.
    assert files == {"threads/aaa-topic.md"}


def test_diversify_on_reaches_across_clusters(isolated_home: Path) -> None:
    _seed_two_equal_relevance_clusters(isolated_home)
    _build_with_keyword_stub("wg")
    hits = search(
        "wg", "alpha", k=3, diversify=True, verbose=Verbosity.QUIET,
    )
    files = {h.file for h in hits}
    # MMR trades a little relevance for coverage, so the second, equally
    # relevant but distinct thread shows up in the same k.
    assert "threads/bbb-topic.md" in files


def test_diversify_suppressed_under_sort_date(isolated_home: Path) -> None:
    # A timeline must keep topically-adjacent messages; MMR is bypassed
    # under sort="date", so the result is the plain relevance set re-sorted
    # by date — here, concentrated on the first cluster like diversify=off.
    _seed_two_equal_relevance_clusters(isolated_home)
    _build_with_keyword_stub("wg")
    hits = search(
        "wg", "alpha", k=3, sort="date", diversify=True,
        verbose=Verbosity.QUIET,
    )
    files = {h.file for h in hits}
    assert files == {"threads/aaa-topic.md"}
