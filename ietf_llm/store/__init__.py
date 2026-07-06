"""Storage seam: the CorpusStore abstraction and its backends.

The read path resolves a corpus's files via `get_corpus_store()` (default
`local` filesystem backend); the opt-in `cloud` backend layers an
object-store blob plane and a compare-and-swap control plane on top. See
`docs/architecture.md` ("The storage seam").
"""

from .corpus import CorpusStore, LocalCorpusStore, VersionVanished, get_corpus_store

__all__ = [
    "CorpusStore",
    "LocalCorpusStore",
    "VersionVanished",
    "get_corpus_store",
]
