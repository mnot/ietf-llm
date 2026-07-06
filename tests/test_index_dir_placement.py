"""With IETF_LLM_INDEX_DIR set, the per-WG embeddings.db is written to (and
read from) the index dir -- separate from the corpus files under the cache
dir -- so a deployment can place the hot index on tmpfs. The corpus files
stay under the cache root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.utils import Verbosity
from ietf_llm.paths import get_cache_dir, get_wg_file_cache_dir

from conftest import write_cache_file


class _Stub:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def test_index_db_lands_in_index_dir(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    idx = tmp_path / "idx"
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(idx))

    write_cache_file(isolated_home, "wg", "drafts/d.txt", "body text\n")
    embeddings._MODEL_CACHE["stub"] = _Stub()  # noqa
    build_index(
        "wg", get_wg_file_cache_dir("wg"), model_name="stub", verbose=Verbosity.QUIET
    )

    # DB under the index dir; not next to the corpus files in the cache dir.
    assert (idx / "wg" / "embeddings.db").is_file()
    assert not (Path(get_cache_dir()) / "wg" / "embeddings.db").exists()
    # And it is searchable through the same indirection.
    assert search("wg", "x", verbose=Verbosity.QUIET)
