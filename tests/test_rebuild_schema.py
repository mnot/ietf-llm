"""A rebuild (model / chunker change, or --rebuild-embeddings) clears the
index meta and rewrites it. The read-only search path checks
schema_version, so a rebuilt index must keep a current schema_version --
otherwise search would reject a freshly rebuilt index as an outdated
schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


class _Stub:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed() -> None:
    embeddings._MODEL_CACHE["stub"] = _Stub()  # noqa


def test_rebuild_keeps_index_searchable(isolated_home: Path) -> None:
    cache = get_wg_file_cache_dir("wg")
    write_cache_file(isolated_home, "wg", "drafts/draft-x.txt", "some body text\n")
    _seed()
    build_index("wg", cache, model_name="stub", verbose=Verbosity.QUIET)
    assert search("wg", "x", verbose=Verbosity.QUIET)

    # Force a rebuild: meta is wiped and rewritten. The read-only search
    # must still accept the index because schema_version was restamped.
    build_index(
        "wg", cache, model_name="stub", rebuild=True, verbose=Verbosity.QUIET
    )
    assert search("wg", "x", verbose=Verbosity.QUIET)
