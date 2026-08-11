"""Turn per-passage search hits into an ordering over RFCs.

Semantic retrieval scores *passages*, but "which RFC answers this" is a
question about documents, and the two orderings differ more than you would
expect. Ranking an RFC by its single strongest chunk makes a document that
merely mentions the query tie one that is about it: rfc.fyi found "HTTP
caching" putting RFC 9111 tenth, behind RFC 3234 (Middleboxes: Taxonomy and
Issues), because 3234 had one section scoring as well as 9111's best did.

The scheme is a port of the one in rfc.fyi's `client.js`, whose coefficients
were swept with `eval/rerank.py` over an 87-query labelled set:

    score = sum(top 3 hit scores) / 3
          + 0.10 * title_overlap
          + 0.02 * log1p(inbound citations)

**The divisor is fixed at three, not the number of hits found.** That is the
whole point: an RFC with one strong passage scores a third of one with three
equally strong, which is what stops a passing mention outranking a document
on its subject. Dividing by `len(top)` would restore exactly the behaviour
this replaces.

The three are the caller's top hits, which are *chunks* — so a long section
split across several of them can occupy more than one slot. That is what the
coefficients were swept against (`scripts/eval_rfc_rank.py`), so the numbers
describe this behaviour and not an idealised one-slot-per-section variant.

**Title overlap is lexical, deliberately.** The corpus embedding never saw
the titles -- only body text was chunked -- so the title is independent
evidence rather than more of the same signal, and "HTTP caching" is the
literal title of the RFC that should come first.

**Citations are a weak prior and want to stay weak.** rfc.fyi measured 0.02
gaining and 0.05 losing 0.11, by promoting well-cited RFCs on citation count
alone. The title weight has a similar cliff: 0.20 (the first guess) scored
*worse* than using no title signal at all.

The trade is stated rather than hidden: aggregating over sections favours
broad documents. "when must a cache not store a response" ranks RFC 9110
above 9111 even though 9111 section 3 is the passage that answers it. That
is the right default for a finder and the wrong one for a passage view, so
the caller chooses -- this is applied for a document-grouped result and not
for a passage-level one.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, Generic, List, Sequence, TypeVar

#: Sections that contribute to a document's score, and the fixed divisor.
DEFAULT_TOP_SECTIONS = 3

#: Swept, not guessed. See the module docstring for what the neighbouring
#: values cost.
DEFAULT_TITLE_WEIGHT = 0.10
DEFAULT_CITATION_WEIGHT = 0.02

#: Query words shorter than this are ignored for title overlap -- "of", "a",
#: "in" appear in a great many titles and carry no topical signal. Mirrors
#: the `PREFIX_LEN` floor the lexical RFC search already uses.
_MIN_TITLE_WORD = 3

_WORD_SPLIT_RE = re.compile(r"\s+")

T = TypeVar("T")


@dataclass(frozen=True)
class RankedDocument(Generic[T]):
    """One document and the hits that earned it its place, best first."""

    doc: str
    score: float
    hits: List[T]


def title_overlap(query: str, title: str) -> float:
    """Fraction of the query's substantive words that appear in `title`.

    Substring rather than token matching, so "caching" finds "Caching" inside
    "HTTP Caching" and "cache" finds it too. Returns 0.0 when the query has
    no word long enough to count, which makes the term inert rather than
    undefined for a query like "TLS 1.3".
    """
    words = [
        w
        for w in _WORD_SPLIT_RE.split(query.lower().strip())
        if len(w) >= _MIN_TITLE_WORD
    ]
    if not words:
        return 0.0
    haystack = (title or "").lower()
    return sum(1 for w in words if w in haystack) / len(words)


def rank_documents(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    query: str,
    hits: Sequence[T],
    *,
    doc_of: Callable[[T], str],
    score_of: Callable[[T], float],
    title_of: Callable[[str], str],
    citations_of: Callable[[str], int],
    top_sections: int = DEFAULT_TOP_SECTIONS,
    title_weight: float = DEFAULT_TITLE_WEIGHT,
    citation_weight: float = DEFAULT_CITATION_WEIGHT,
) -> List[RankedDocument[T]]:
    """Group `hits` by document and order the documents by the scheme above.

    Each document keeps *all* its hits, ordered best first, so a caller can
    show the sections that matched; only the top `top_sections` of them
    contribute to the ordering. The lookups are injected rather than imported
    so this stays a pure function over its inputs -- the metadata lives in
    the `_rfc/` mirror, which the caller owns.
    """
    grouped: Dict[str, List[T]] = {}
    for hit in hits:
        grouped.setdefault(doc_of(hit), []).append(hit)

    ranked: List[RankedDocument[T]] = []
    for doc, doc_hits in grouped.items():
        ordered = sorted(doc_hits, key=score_of, reverse=True)
        # Fixed divisor: see the module docstring. `top_sections` is both how
        # many count and what we divide by.
        passages = sum(score_of(h) for h in ordered[:top_sections]) / top_sections
        score = (
            passages
            + title_weight * title_overlap(query, title_of(doc))
            + citation_weight * math.log1p(max(0, citations_of(doc)))
        )
        ranked.append(RankedDocument(doc=doc, score=score, hits=ordered))

    # Ties broken by document name so the order is stable run to run; a caller
    # rendering "top 20 of N" must not shuffle between identical queries.
    ranked.sort(key=lambda r: (-r.score, r.doc))
    return ranked
