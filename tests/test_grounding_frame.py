"""Tests for the grounding frame.

When a search or read_topic result touches a thread big enough that its
consensus / individual positions shouldn't be read off snippets, the output
gets an interpretive frame prepended at the TOP: it states the principle
inline (IETF consensus is chair-declared, not counted; narrative is what was
said, not decided) so there is nothing to skip and no separate tool call to
make. Unit tests stub _thread_sizes for precise threshold checks; the
integration tests drive tool_search / tool_read_topic against a seeded digest.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

# Stable marker for "the frame is present" — distinctive opening words.
_MARKER = "Before characterising any decision"


# --- threshold logic (stubbed _thread_sizes) -------------------------------


def test_frame_fires_over_msg_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp.common, "_thread_sizes", lambda wg: {"threads/big.md": ("325", "62")}
    )
    out = mcp.common._grounding_frame("wg", ["threads/big.md"])
    assert _MARKER in out
    assert "read_ietf_interpretation_norms" in out
    assert "325 msgs, 62 participants" in out
    assert "measure of support" in out


def test_frame_fires_on_participants_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Few messages but many distinct participants is still a real debate.
    monkeypatch.setattr(
        mcp.common, "_thread_sizes", lambda wg: {"threads/wide.md": ("12", "9")}
    )
    assert "threads/wide.md" in mcp.common._grounding_frame("wg", ["threads/wide.md"])


def test_frame_quiet_under_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp.common, "_thread_sizes", lambda wg: {"threads/small.md": ("6", "3")}
    )
    assert mcp.common._grounding_frame("wg", ["threads/small.md"]) == ""


def test_frame_ignores_non_thread_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # A draft file never triggers the frame even if (somehow) sized.
    monkeypatch.setattr(
        mcp.common, "_thread_sizes", lambda wg: {"drafts/draft-x-00.txt": ("99", "99")}
    )
    assert mcp.common._grounding_frame("wg", ["drafts/draft-x-00.txt"]) == ""


def test_frame_picks_largest_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp.common,
        "_thread_sizes",
        lambda wg: {"threads/a.md": ("25", "8"), "threads/b.md": ("400", "70")},
    )
    out = mcp.common._grounding_frame("wg", ["threads/a.md", "threads/b.md"])
    assert "threads/b.md" in out and "400 msgs" in out


def test_frame_tolerates_unparseable_size_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed digest row (non-numeric Msgs/Participants) parses to 0 via
    # _first_int and falls below threshold rather than crashing.
    monkeypatch.setattr(
        mcp.common,
        "_thread_sizes",
        lambda wg: {"threads/junk.md": ("(no subject)", "Selection, Config...")},
    )
    assert mcp.common._grounding_frame("wg", ["threads/junk.md"]) == ""


# --- integration through the tools -----------------------------------------


class _StubModel:
    """Constant-vector model — lets search() run end-to-end without a real
    embedding backend (cosine 1.0 between any pair; ranking is arbitrary)."""

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_stub(wg: str) -> None:
    cache = get_wg_file_cache_dir(wg)
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


def _threads_digest(rows: str) -> str:
    return (
        "# Threads\n\n"
        "| Subject | Msgs | Participants | First | Last | File |\n"
        "|---|---|---|---|---|---|\n" + rows
    )


def _seed_big_thread(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-04-09-wglc-mldsa.md",
        "# WGLC for ML-DSA\n\n## Messages\n\n"
        "### [1] 2026-04-09 09:00 — Alice\n\nML-DSA WGLC opening.\n\n"
        "### [2] 2026-04-10 09:00 — Bob\n\nML-DSA concerns.\n",
    )
    # The frame reads thread size from the digest, not the raw file.
    write_cache_file(
        home, "wg", "digests/threads.md",
        _threads_digest(
            "| WGLC for ML-DSA | 325 | 62 | 2026-04-09 | 2026-05-01 | "
            "`threads/2026-04-09-wglc-mldsa.md` |\n"
        ),
    )


def _seed_small_thread(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-04-09-minor.md",
        "# Minor\n\n## Messages\n\n"
        "### [1] 2026-04-09 09:00 — Alice\n\nML-DSA quick note.\n",
    )
    write_cache_file(
        home, "wg", "digests/threads.md",
        _threads_digest(
            "| Minor | 4 | 2 | 2026-04-09 | 2026-04-09 | "
            "`threads/2026-04-09-minor.md` |\n"
        ),
    )


def test_tool_search_prepends_grounding_frame(isolated_home: Path) -> None:
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.search.tool_search("wg", "ML-DSA", k=10)
    assert _MARKER in out
    assert "325 msgs, 62 participants" in out
    # Frame is at the TOP — before the first rendered hit.
    assert out.index(_MARKER) < out.index("score=")


def test_tool_search_no_frame_for_small_thread(isolated_home: Path) -> None:
    _seed_small_thread(isolated_home)
    _build_with_stub("wg")
    assert _MARKER not in mcp.search.tool_search("wg", "ML-DSA", k=10)


def test_tool_search_frame_survives_group_by_file(isolated_home: Path) -> None:
    # group_by="file" collapses _render_hits to file rows; the frame is
    # prepended independently and must still fire for the big thread.
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.search.tool_search("wg", "ML-DSA", k=10, group_by="file")
    assert _MARKER in out and "325 msgs, 62 participants" in out


def test_read_topic_prepends_grounding_frame(isolated_home: Path) -> None:
    # read_topic is the narrative tool that failed in the field — it must get
    # the frame, and before the messages.
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.topic.tool_read_topic("wg", "ML-DSA", k=10)
    assert _MARKER in out
    # Frame precedes the first chronological message header (`## [1] …`).
    assert out.index(_MARKER) < out.index("## [1]")


def test_read_topic_no_frame_for_small_thread(isolated_home: Path) -> None:
    _seed_small_thread(isolated_home)
    _build_with_stub("wg")
    assert _MARKER not in mcp.topic.tool_read_topic("wg", "ML-DSA", k=10)


def test_find_related_has_no_grounding_frame(isolated_home: Path) -> None:
    # The frame lives in tool_search / tool_read_topic, not the shared
    # _render_hits, so find_related (which also uses _render_hits) is unaffected.
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.search.tool_find_related("wg", "threads/2026-04-09-wglc-mldsa.md", 1)
    assert _MARKER not in out
