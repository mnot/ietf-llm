"""`get_messages` fetches by (file, chunk_idx) pair; SQLite rejects a single
OR chain of about a thousand terms ("Expression tree is too large"), so a
long thread must be batched."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.embeddings.storage import get_messages
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def test_a_very_long_key_list_is_batched(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-t.md",
        "# T\n\n## Messages\n\n"
        "### [1] 2026-01-01 09:00 — A\n\nFirst.\n\n"
        "### [2] 2026-01-02 09:00 — B (reply to [1])\n\nSecond.\n",
    )
    embeddings._MODEL_CACHE["stub"] = _StubModel()
    build_index("wg", get_wg_file_cache_dir("wg"), model_name="stub",
                verbose=Verbosity.QUIET)

    real = get_messages("wg", [("threads/2026-01-01-t.md", 0)])
    assert real, "fixture should yield chunk_idx 0 of the thread"
    key = next(iter(real))

    # Far past the ~1000-term expression-depth limit, real keys at both ends.
    keys = [key] + [(f"threads/none-{i}.md", i) for i in range(2500)] + [key]
    got = get_messages("wg", keys)
    assert got == real
