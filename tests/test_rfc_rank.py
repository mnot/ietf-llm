"""Document-level ranking for RFC search (#230).

The behaviour worth pinning is the fixed divisor: a document with one strong
section must not tie one with three, because dividing by the number of
sections found is exactly the scheme this replaces.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List

from ietf_llm.singletons.rfc_rank import (
    DEFAULT_CITATION_WEIGHT,
    DEFAULT_TITLE_WEIGHT,
    rank_documents,
    title_overlap,
)


@dataclass(frozen=True)
class _Hit:
    rfc: str
    score: float


TITLES: Dict[str, str] = {
    "RFC9111": "HTTP Caching",
    "RFC3234": "Middleboxes: Taxonomy and Issues",
}
CITES: Dict[str, int] = {"RFC9111": 100, "RFC3234": 10}


def _rank(query: str, hits: List[_Hit], **kw: object) -> List[str]:
    ranked = rank_documents(
        query,
        hits,
        doc_of=lambda h: h.rfc,
        score_of=lambda h: h.score,
        title_of=lambda d: TITLES.get(d, ""),
        citations_of=lambda d: CITES.get(d, 0),
        **kw,  # type: ignore[arg-type]
    )
    return [r.doc for r in ranked]


def test_three_good_sections_beat_one_equally_good_one() -> None:
    # The failure that motivated the change: 3234 has one section scoring as
    # well as 9111's best, and used to tie it.
    hits = [
        _Hit("RFC9111", 0.80),
        _Hit("RFC9111", 0.78),
        _Hit("RFC9111", 0.77),
        _Hit("RFC3234", 0.80),
    ]
    assert _rank("http caching", hits)[0] == "RFC9111"


def test_divisor_is_fixed_not_the_number_found() -> None:
    """One section at 0.9 must score 0.3 from passages, not 0.9."""
    ranked = rank_documents(
        "zzz",  # no title overlap
        [_Hit("RFC9111", 0.9)],
        doc_of=lambda h: h.rfc,
        score_of=lambda h: h.score,
        title_of=lambda d: "",
        citations_of=lambda d: 0,
    )
    assert ranked[0].score == 0.9 / 3


def test_title_overlap_is_the_documented_fraction() -> None:
    assert title_overlap("http caching", "HTTP Caching") == 1.0
    assert title_overlap("http caching directives", "HTTP Caching") == 2 / 3
    assert title_overlap("quic", "HTTP Caching") == 0.0
    # Words below the length floor are ignored, not counted as misses.
    assert title_overlap("of a caching", "HTTP Caching") == 1.0
    # No substantive word at all leaves the term inert rather than undefined.
    assert title_overlap("a of", "HTTP Caching") == 0.0


def test_score_is_the_documented_sum() -> None:
    ranked = rank_documents(
        "http caching",
        [_Hit("RFC9111", 0.6), _Hit("RFC9111", 0.3)],
        doc_of=lambda h: h.rfc,
        score_of=lambda h: h.score,
        title_of=lambda d: TITLES[d],
        citations_of=lambda d: CITES[d],
    )
    expected = (
        (0.6 + 0.3) / 3
        + DEFAULT_TITLE_WEIGHT * 1.0
        + DEFAULT_CITATION_WEIGHT * math.log1p(100)
    )
    assert ranked[0].score == expected


def test_hits_are_kept_and_ordered_best_first() -> None:
    ranked = rank_documents(
        "http caching",
        [_Hit("RFC9111", 0.3), _Hit("RFC9111", 0.9), _Hit("RFC9111", 0.6)],
        doc_of=lambda h: h.rfc,
        score_of=lambda h: h.score,
        title_of=lambda d: "",
        citations_of=lambda d: 0,
    )
    # All four survive for rendering even though only three are scored.
    assert [h.score for h in ranked[0].hits] == [0.9, 0.6, 0.3]


def test_citations_do_not_overturn_passages() -> None:
    """A weak prior stays weak: 3234's ten-fold citation deficit must not be
    what decides, and a well-cited RFC must not win on citations alone."""
    hits = [_Hit("RFC3234", 0.90), _Hit("RFC9111", 0.30)]
    assert _rank("zzz", hits)[0] == "RFC3234"


def test_ties_break_stably_by_name() -> None:
    hits = [_Hit("RFC0002", 0.5), _Hit("RFC0001", 0.5)]
    order = _rank(
        "zzz", hits
    )  # equal scores, no title overlap, no citations for either
    assert order == ["RFC0001", "RFC0002"]
