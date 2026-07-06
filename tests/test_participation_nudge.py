"""Tests for the write-side participation nudge.

The read tools surface quotable thread / issue material; the moment a caller
has that in hand is when a drafting decision gets made. So — mirroring the
read-side grounding frame — the material-acquisition tools append a one-line
nudge pointing at `read_ietf_participation_norms`. It fires only for
`threads/` / `issues/` content (what a reply quotes), never for drafts / RFCs /
digests, and a tool that fans out to another read tool (get_chunks_batch →
get_chunk) emits a single nudge, not one per chunk.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file

# Distinctive opening words for the nudge — the "is it present" marker.
_MARKER = "About to draft a contribution"


# --- unit: the gating predicate ---------------------------------------------


def test_nudge_fires_for_thread_and_issue_files() -> None:
    assert _MARKER in mcp.common._participation_nudge("threads/2026-04-09-x.md")
    assert _MARKER in mcp.common._participation_nudge("issues/org-repo/12.md")
    # Case-insensitive on the prefix.
    assert _MARKER in mcp.common._participation_nudge("Threads/2026-04-09-x.md")


def test_nudge_silent_for_non_quotable_files() -> None:
    for path in (
        "drafts/draft-foo-00.txt",
        "rfc/rfc9110.txt",
        "digests/threads.md",
        "minutes/2026-03.md",
    ):
        assert mcp.common._participation_nudge(path) == ""


def test_nudge_accepts_a_list_and_fires_if_any_qualifies() -> None:
    assert _MARKER in mcp.common._participation_nudge(
        ["drafts/draft-foo-00.txt", "threads/2026-04-09-x.md"]
    )
    assert mcp.common._participation_nudge(["drafts/a.txt", "rfc/b.txt"]) == ""
    assert mcp.common._participation_nudge([]) == ""


def test_nudge_points_at_participation_norms_only() -> None:
    # It is the WRITE-side gate: it must name the participation norms and must
    # not masquerade as the read-side interpretation gate.
    out = mcp.common._participation_nudge("threads/2026-04-09-x.md")
    assert "read_ietf_participation_norms" in out
    assert "read_ietf_interpretation_norms" not in out


# --- unit: the append helper + batch-suppression flag -----------------------


def test_append_nudge_adds_footer_when_it_fires() -> None:
    body = "some message body"
    out = mcp.common._append_participation_nudge("threads/2026-04-09-x.md", body)
    assert out.startswith(body)
    assert _MARKER in out
    # Footer, i.e. after the body.
    assert out.index(body) < out.index(_MARKER)


def test_append_nudge_noop_for_non_quotable_file() -> None:
    body = "draft text"
    assert mcp.common._append_participation_nudge("drafts/d-00.txt", body) == body


def test_append_nudge_enabled_false_suppresses() -> None:
    body = "body"
    assert (
        mcp.common._append_participation_nudge(
            "threads/2026-04-09-x.md", body, enabled=False
        )
        == body
    )


# --- integration through the read tools -------------------------------------


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_stub(wg: str) -> None:
    cache = get_wg_file_cache_dir(wg)
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


def _seed_thread(home: Path) -> None:
    write_cache_file(
        home, "wg", "threads/2026-04-10-mlkem.md",
        (
            "# MLKEM\n\n## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\n"
            "_Subject:_ MLKEM\n\nMLKEM opening message body.\n\n"
            "### [2] 2026-04-11 09:00 — Bob\n\n"
            "_Subject:_ Re: MLKEM\n\nMLKEM reply body from Bob.\n"
        ),
    )


def test_read_topic_appends_nudge_after_messages(isolated_home: Path) -> None:
    _seed_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.topic.tool_read_topic("wg", "MLKEM", k=10)
    assert _MARKER in out
    # It is a footer — after the last rendered message header.
    assert out.rindex(_MARKER) > out.rindex("## [")


def test_get_chunk_appends_nudge_for_thread_chunk(isolated_home: Path) -> None:
    _seed_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.chunks.tool_get_chunk("wg", "threads/2026-04-10-mlkem.md", 1)
    assert _MARKER in out


def test_get_chunk_silent_for_draft_chunk(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "drafts/draft-foo-00.txt", "MLKEM draft text. " * 20
    )
    _build_with_stub("wg")
    out = mcp.chunks.tool_get_chunk("wg", "drafts/draft-foo-00.txt", 0)
    assert _MARKER not in out


def test_get_chunks_batch_emits_single_nudge(isolated_home: Path) -> None:
    # The batch fans out to get_chunk per request; the nudge must appear ONCE
    # for the whole batch, not once per chunk.
    _seed_thread(isolated_home)
    _build_with_stub("wg")
    out = mcp.chunks.tool_get_chunks_batch(
        "wg",
        [
            {"file": "threads/2026-04-10-mlkem.md", "chunk_idx": 1},
            {"file": "threads/2026-04-10-mlkem.md", "chunk_idx": 2},
        ],
    )
    assert out.count(_MARKER) == 1
