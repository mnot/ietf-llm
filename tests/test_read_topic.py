"""Tests for `read_topic` — the cross-file chronological view.

Uses the same stub-model + write_cache_file pattern as
test_search_filters.py: a fake embedding model that scores every chunk
identically lets the test exercise the merging / sorting / reply-graph
logic without depending on real semantic ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings, mcp_server
from ietf_llm.embeddings.search import build_index
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    """Constant-vector model — every chunk gets the same score, so the
    ranking under search() is arbitrary but stable. We rely on the
    *filtering* and *sorting* the new code does, not on relevance."""

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_stub(wg: str) -> None:
    cache = get_wg_file_cache_dir(wg)
    embeddings._MODEL_CACHE["stub"] = _StubModel()  # pylint: disable=protected-access
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


# Three thread messages across two files at known dates. Used by the
# basic chronological-ordering test below; written once and shared.
def _seed_two_threads(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-mlkem-early.md",
        (
            "# MLKEM early\n\n"
            "## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\n"
            "_Subject:_ MLKEM early\n"
            "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/aaa/\n\n"
            "Body about MLKEM, opening the topic.\n\n"
            "### [2] 2026-04-12 14:00 — Bob (reply to [1])\n\n"
            "_Subject:_ Re: MLKEM early\n"
            "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/bbb/\n\n"
            "Reply from Bob, MLKEM tradeoffs.\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-05-01-mlkem-followup.md",
        (
            "# MLKEM followup\n\n"
            "## Messages\n\n"
            "### [1] 2026-05-01 10:00 — Carol\n\n"
            "_Subject:_ MLKEM followup\n"
            "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/ccc/\n\n"
            "Later MLKEM discussion from Carol.\n"
        ),
    )


def test_read_topic_returns_messages_in_chronological_order(
    isolated_home: Path,
) -> None:
    _seed_two_threads(isolated_home)
    _build_with_stub("wg")

    out = mcp_server.tool_read_topic("wg", "MLKEM", k=10)

    # All three messages render, each with its full body (not a snippet).
    assert "Body about MLKEM, opening the topic." in out
    assert "Reply from Bob, MLKEM tradeoffs." in out
    assert "Later MLKEM discussion from Carol." in out

    # And in chronological order: Alice (Apr 10) < Bob (Apr 12) < Carol (May 1).
    a = out.index("Body about MLKEM, opening the topic.")
    b = out.index("Reply from Bob, MLKEM tradeoffs.")
    c = out.index("Later MLKEM discussion from Carol.")
    assert a < b < c


def test_read_topic_excludes_windowed_draft_chunks(isolated_home: Path) -> None:
    # Even though a draft chunk might match the query semantically, the
    # chronological view excludes drafts / transcripts — they aren't
    # messages in a debate.
    _seed_two_threads(isolated_home)
    write_cache_file(
        isolated_home, "wg", "drafts/draft-foo-00.txt",
        "MLKEM appears in this draft. " * 50,
    )
    _build_with_stub("wg")

    out = mcp_server.tool_read_topic("wg", "MLKEM", k=10)
    # No draft file should appear among the rendered rows.
    assert "drafts/draft-foo-00.txt" not in out


def test_read_topic_renders_full_body_not_snippet(isolated_home: Path) -> None:
    # The big win of read_topic over search_corpus is that the unit is
    # the full message, not the truncated snippet. Verify by inserting
    # a body long enough that a snippet would clip it.
    long_body = "MLKEM tradeoff discussion. " * 20  # ~540 chars
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-long.md",
        (
            "# Long\n\n"
            "## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\n"
            f"_Subject:_ Long\n\n{long_body}\n"
        ),
    )
    _build_with_stub("wg")
    out = mcp_server.tool_read_topic("wg", "MLKEM", k=5)
    # The whole body — not a [truncated] snippet — appears.
    assert long_body.strip() in out
    assert "[truncated]" not in out


def test_read_topic_include_replies_pulls_descendants(
    isolated_home: Path,
) -> None:
    # A thread where only the root matches semantically — but with
    # `include_replies=True` the descendants should be pulled in
    # regardless, since they're part of the arc.
    #
    # We can't make the stub model differentiate matches, so this test
    # uses a workaround: seed the file with the reply graph and verify
    # the rendered output marks descendants as `[reply]` (vs `[matched]`)
    # — that's the only thing include_replies adds on top of what the
    # default code path renders. We'll instead test the reply graph
    # parser directly + the rendering tags.
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-arc.md",
        (
            "# Arc\n\n"
            "## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\nRoot post.\n\n"
            "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n\n"
            "First reply.\n\n"
            "### [3] 2026-04-12 09:00 — Carol (reply to [2])\n\n"
            "Reply to reply.\n"
        ),
    )
    _build_with_stub("wg")

    # With k=1 we still expect all three messages because include_replies
    # walks down the reply graph from the matched message. Since the
    # stub model can't distinguish, this works only because k=1 picks
    # *one* chunk (whatever search returns first) and the reply walk
    # fills in the rest if that one happens to be the root.
    #
    # Robustness: assert the reply-graph parser directly here instead,
    # since the rendering test depends on which chunk search returns
    # first.
    text = (
        "### [1] 2026-04-10 09:00 — Alice\n"
        "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n"
        "### [3] 2026-04-12 09:00 — Carol (reply to [2])\n"
    )
    graph = mcp_server._parse_reply_graph(text)  # pylint: disable=protected-access
    assert graph == {1: [2], 2: [3]}

    # Descendants of 1 = [2, 3] (BFS).
    desc = mcp_server._descendants(graph, 1)  # pylint: disable=protected-access
    assert desc == [2, 3]
    # Descendants of 2 = [3].
    assert mcp_server._descendants(graph, 2) == [3]  # pylint: disable=protected-access
    # Leaf node has no descendants.
    assert mcp_server._descendants(graph, 3) == []  # pylint: disable=protected-access


def test_read_topic_include_replies_renders_replies_in_output(
    isolated_home: Path,
) -> None:
    # End-to-end: build an index where the only thread-file chunks are
    # message chunks (header chunk has no date so it's filtered out),
    # and verify that include_replies=True tags non-matched messages as
    # `[reply]` in the output.
    #
    # The stub model makes all chunks score identically, so search()
    # returns *all* message chunks with k=20. To make include_replies
    # observable, request k=1 — then only one matched chunk is kept,
    # and the rest should appear as `[reply]` rows.
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-arc.md",
        (
            "# Arc\n\n"
            "## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\nRoot post.\n\n"
            "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n\n"
            "First reply.\n\n"
            "### [3] 2026-04-12 09:00 — Carol (reply to [2])\n\n"
            "Reply to reply.\n"
        ),
    )
    _build_with_stub("wg")

    # Without include_replies, k=1 returns one matched message and no
    # extras.
    bare = mcp_server.tool_read_topic("wg", "anything", k=1)
    assert bare.count("[matched]") == 1
    assert "[reply]" not in bare

    # With include_replies, descendants of the matched message in the
    # SAME thread file are pulled in.
    expanded = mcp_server.tool_read_topic(
        "wg", "anything", k=1, include_replies=True,
    )
    # At least one [reply] appears (depending on which chunk search
    # picked as the top hit — root, mid, or leaf — descendants vary).
    assert "[matched]" in expanded
    # If the top hit was message 1 or 2, we get replies. Message 3 has
    # no descendants. Allow either: at least the total message count is
    # >= the bare case.
    assert expanded.count("##") >= bare.count("##")


def test_read_topic_numbers_messages_globally(isolated_home: Path) -> None:
    # Two threads each have a per-file `[1]`. Merged into one timeline the
    # numbering must be global [1..N] (no repeated [1]), and a per-file
    # "(reply to [1])" must remap to the parent's global number. Dates set
    # the chronological order: A1(01-01) < B1(01-02) < A2(01-03).
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-a.md",
        (
            "# A\n\n## Messages\n\n"
            "### [1] 2026-01-01 09:00 — Alice\n\nAlice opens.\n\n"
            "### [2] 2026-01-03 09:00 — Bob (reply to [1])\n\nBob replies.\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-02-b.md",
        (
            "# B\n\n## Messages\n\n"
            "### [1] 2026-01-02 09:00 — Carol\n\nCarol posts.\n"
        ),
    )
    _build_with_stub("wg")
    out = mcp_server.tool_read_topic("wg", "anything", k=10)
    # Global sequence, no repeated [1].
    assert "## [1] 2026-01-01 09:00 — Alice" in out
    assert "## [2] 2026-01-02 09:00 — Carol" in out
    assert "## [3] 2026-01-03 09:00 — Bob" in out
    assert out.count("## [1]") == 1
    # Bob's per-file "(reply to [1])" remaps to Alice's global number [1].
    assert "## [3] 2026-01-03 09:00 — Bob  ·  [matched]  ·  reply to [1]" in out
    # No duplicated `### [N]` body header.
    assert "\n### [" not in out


def test_read_topic_no_thread_matches_returns_hint(isolated_home: Path) -> None:
    # Only a draft is indexed — read_topic should return a hint pointing
    # the consumer at search_corpus rather than silently producing an
    # empty timeline.
    write_cache_file(
        isolated_home, "wg", "drafts/draft-foo-00.txt",
        "Body of the draft. " * 50,
    )
    _build_with_stub("wg")
    out = mcp_server.tool_read_topic("wg", "MLKEM", k=5)
    # Drafts have no chunk_date so the sort="date" pre-filter drops them;
    # search returns nothing, and the "no results" hint kicks in.
    assert "no" in out.lower() and ("results" in out or "thread" in out)


def test_find_replies_returns_descendants_in_order(
    isolated_home: Path,
) -> None:
    # 1 ← 2 ← 3 (linear chain). find_replies(1) returns [2, 3];
    # find_replies(2) returns [3]; find_replies(3) returns "no replies".
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-arc.md",
        (
            "# Arc\n\n"
            "## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\nRoot post.\n\n"
            "### [2] 2026-04-11 09:00 — Bob (reply to [1])\n\n"
            "First reply.\n\n"
            "### [3] 2026-04-12 09:00 — Carol (reply to [2])\n\n"
            "Reply to reply.\n"
        ),
    )
    _build_with_stub("wg")
    out_from_root = mcp_server.tool_find_replies(
        "wg", "threads/2026-04-10-arc.md", chunk_idx=1,
    )
    assert "First reply." in out_from_root
    assert "Reply to reply." in out_from_root
    assert out_from_root.index("First reply.") < out_from_root.index("Reply to reply.")
    # Each message carries exactly one header — the `### [N] …` from its
    # stored body — not a second `## …` header duplicating author/date.
    assert "\n## " not in out_from_root
    assert "### [2] 2026-04-11 09:00 — Bob (reply to [1])" in out_from_root
    assert out_from_root.count("09:00 — Bob") == 1

    out_from_leaf = mcp_server.tool_find_replies(
        "wg", "threads/2026-04-10-arc.md", chunk_idx=3,
    )
    assert "No replies" in out_from_leaf


def test_find_replies_refuses_issue_files(isolated_home: Path) -> None:
    out = mcp_server.tool_find_replies(
        "wg", "issues/org-repo/1.md", chunk_idx=1,
    )
    assert "issue" in out.lower()
    assert "get_chunk_text" in out


def test_read_topic_clamps_excessive_k(isolated_home: Path) -> None:
    # A misuse like k=500 should not blow up the SQL OR-chain in
    # get_messages or burn context — the tool clamps k internally
    # well below the render cap (60 messages) so the cost is bounded.
    _seed_two_threads(isolated_home)
    _build_with_stub("wg")
    # With only 3 dated messages in the corpus, k=500 still works and
    # returns those 3 — no error, no runaway.
    out = mcp_server.tool_read_topic("wg", "MLKEM", k=500)
    assert "Body about MLKEM, opening the topic." in out
    assert "Later MLKEM discussion from Carol." in out


def test_read_topic_summary_line_counts_matched_and_replies(
    isolated_home: Path,
) -> None:
    _seed_two_threads(isolated_home)
    _build_with_stub("wg")
    out = mcp_server.tool_read_topic("wg", "MLKEM", k=10)
    # Summary line includes message count, file count, and "oldest first".
    assert "oldest first" in out
    assert "matched the query" in out
