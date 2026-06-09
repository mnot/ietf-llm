"""Tests for the search_corpus grounding footer.

The footer fires when a search result touches a thread big enough that its
consensus / individual positions shouldn't be read off snippets. It steers
the caller to read_ietf_interpretation_norms + the chair's actual words, and
explicitly demotes tally_positions' +1/-1 count (a keyword heuristic, not
sentiment analysis). Unit tests stub _thread_sizes for precise threshold
checks; the integration tests drive tool_search against a seeded digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

import pytest

from ietf_llm import embeddings, mcp_server
from ietf_llm.embeddings.search import build_index
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


class _Hit:
    """Minimal stand-in for embeddings.search.Hit — the footer reads .file."""

    def __init__(self, file: str) -> None:
        self.file = file


# --- threshold logic (stubbed _thread_sizes) -------------------------------


def test_footer_fires_over_msg_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server, "_thread_sizes", lambda wg: {"threads/big.md": ("325", "62")}
    )
    out = mcp_server._grounding_footer("wg", [_Hit("threads/big.md")])
    assert "read_ietf_interpretation_norms" in out
    assert "325 msgs, 62 participants" in out
    assert "not a measure of support" in out


def test_footer_fires_on_participants_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Few messages but many distinct participants is still a real debate.
    monkeypatch.setattr(
        mcp_server, "_thread_sizes", lambda wg: {"threads/wide.md": ("12", "9")}
    )
    assert "threads/wide.md" in mcp_server._grounding_footer("wg", [_Hit("threads/wide.md")])


def test_footer_quiet_under_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server, "_thread_sizes", lambda wg: {"threads/small.md": ("6", "3")}
    )
    assert mcp_server._grounding_footer("wg", [_Hit("threads/small.md")]) == ""


def test_footer_ignores_non_thread_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # A draft hit never triggers the footer even if (somehow) sized.
    monkeypatch.setattr(
        mcp_server, "_thread_sizes", lambda wg: {"drafts/draft-x-00.txt": ("99", "99")}
    )
    assert mcp_server._grounding_footer("wg", [_Hit("drafts/draft-x-00.txt")]) == ""


def test_footer_picks_largest_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_thread_sizes",
        lambda wg: {"threads/a.md": ("25", "8"), "threads/b.md": ("400", "70")},
    )
    out = mcp_server._grounding_footer("wg", [_Hit("threads/a.md"), _Hit("threads/b.md")])
    assert "threads/b.md" in out and "400 msgs" in out


def test_footer_tolerates_unparseable_size_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    # A malformed digest row (non-numeric Msgs/Participants) parses to 0 via
    # _first_int and falls below threshold rather than crashing.
    monkeypatch.setattr(
        mcp_server,
        "_thread_sizes",
        lambda wg: {"threads/junk.md": ("(no subject)", "Selection, Config...")},
    )
    assert mcp_server._grounding_footer("wg", [_Hit("threads/junk.md")]) == ""


# --- integration through tool_search ---------------------------------------


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
    # The footer reads thread size from the digest, not the raw file.
    write_cache_file(
        home, "wg", "digests/threads.md",
        _threads_digest(
            "| WGLC for ML-DSA | 325 | 62 | 2026-04-09 | 2026-05-01 | "
            "`threads/2026-04-09-wglc-mldsa.md` |\n"
        ),
    )


def test_tool_search_appends_grounding_footer(isolated_home: Path) -> None:
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp_server.tool_search("wg", "ML-DSA", k=10)
    assert "_Grounding:" in out
    assert "read_ietf_interpretation_norms" in out
    assert "325 msgs, 62 participants" in out


def test_tool_search_no_footer_for_small_thread(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-09-minor.md",
        "# Minor\n\n## Messages\n\n"
        "### [1] 2026-04-09 09:00 — Alice\n\nML-DSA quick note.\n",
    )
    write_cache_file(
        isolated_home, "wg", "digests/threads.md",
        _threads_digest(
            "| Minor | 4 | 2 | 2026-04-09 | 2026-04-09 | "
            "`threads/2026-04-09-minor.md` |\n"
        ),
    )
    _build_with_stub("wg")
    assert "_Grounding:" not in mcp_server.tool_search("wg", "ML-DSA", k=10)


def test_tool_search_footer_survives_group_by_file(isolated_home: Path) -> None:
    # group_by="file" collapses _render_hits to file rows; the footer is
    # appended independently and must still fire for the big thread.
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp_server.tool_search("wg", "ML-DSA", k=10, group_by="file")
    assert "_Grounding:" in out
    assert "325 msgs, 62 participants" in out


def test_find_related_has_no_grounding_footer(isolated_home: Path) -> None:
    # The footer lives in tool_search, not the shared _render_hits, so
    # find_related (which also uses _render_hits) must not carry it.
    _seed_big_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp_server.tool_find_related("wg", "threads/2026-04-09-wglc-mldsa.md", 1)
    assert "_Grounding:" not in out
