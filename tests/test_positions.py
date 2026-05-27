"""Tests for the heuristic position extractor (`positions.py`) and
the `tally_positions` MCP tool.

The extractor's contract is high precision over high recall: it should
only return support / oppose when the canonical phrasing is present.
Sentiment-only or technical objections legitimately fall through to
no-position; the renderer surfaces the coverage % so the consumer can
see how much the heuristic missed.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import mcp_server
from ietf_llm.positions import (
    extract_position,
    file_supports_tally,
    tally_thread,
)
from ietf_llm.utils import get_wg_file_cache_dir

from conftest import write_cache_file


# --- extract_position ------------------------------------------------------


def test_bare_plus_one_counts_as_support() -> None:
    label, conf, excerpt = extract_position("+1\n")
    assert label == "support"
    assert conf == "high"
    assert "+1" in excerpt


def test_bare_minus_one_counts_as_oppose() -> None:
    label, _conf, _excerpt = extract_position("-1\n")
    assert label == "oppose"


def test_i_support_phrase() -> None:
    label, conf, _excerpt = extract_position("I support adoption of this draft.\n")
    assert label == "support"
    assert conf == "high"


def test_i_object_phrase() -> None:
    label, _conf, _excerpt = extract_position(
        "I object to this draft as written.\n"
    )
    assert label == "oppose"


def test_conditional_support_gets_its_own_bucket() -> None:
    label, conf, _excerpt = extract_position(
        "I support this with the change that we remove section 4.\n"
    )
    assert label == "conditional"
    assert conf == "high"


def test_lgtm_counts_as_support() -> None:
    label, _conf, _excerpt = extract_position("LGTM\n")
    assert label == "support"


def test_discuss_counts_as_oppose() -> None:
    # IESG ballot position. Treated as opposition for tally purposes —
    # a DISCUSS holds publication.
    label, _conf, _excerpt = extract_position("DISCUSS\n\nThis draft has...\n")
    assert label == "oppose"


def test_quoted_plus_one_does_not_count() -> None:
    # The author is quoting someone else's +1, then disagreeing.
    body = "> +1 to publishing as-is\n\nI object — premature.\n"
    label, _conf, _excerpt = extract_position(body)
    assert label == "oppose"


def test_subject_metadata_line_not_matched_as_position() -> None:
    # The thread file format puts `_Subject:_ Re: I support adoption` at
    # the top of each message section. The extractor must strip that
    # so it doesn't misread the subject as the body's position.
    body = (
        "_Subject:_ Re: I support adoption\n"
        "_Archived-At:_ https://mailarchive.ietf.org/foo/\n"
        "\n"
        "This is a technical question, not a position statement.\n"
    )
    label, _conf, _excerpt = extract_position(body)
    assert label == "no-position"


def test_technical_objection_falls_through_to_no_position() -> None:
    # The heuristic is intentionally narrow. A long technical
    # objection without "I object" / "DISCUSS" / "-1" goes to
    # no-position — that's honest, not a bug.
    body = (
        "Section 4 has an interoperability problem: the negotiation "
        "step assumes both peers advertise the extension, which my "
        "implementation can't guarantee.\n"
    )
    label, _conf, _excerpt = extract_position(body)
    assert label == "no-position"


def test_no_position_message_returns_empty_excerpt() -> None:
    _label, conf, excerpt = extract_position("Just a clarifying question.\n")
    assert conf == ""
    assert excerpt == ""


# --- tally_thread + render -------------------------------------------------


def _wglc_thread() -> str:
    return (
        "# WGLC: draft-foo\n\n"
        "**Messages:** 5\n\n"
        "## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Chair Person\n\n"
        "_Subject:_ WGLC: draft-foo\n\n"
        "Please send your support/objections by 2026-04-24.\n\n"
        "### [2] 2026-04-11 10:00 — Alice (Chair)\n\n"
        "+1\n\n"
        "### [3] 2026-04-12 11:00 — Bob\n\n"
        "I support adoption of this draft.\n\n"
        "### [4] 2026-04-13 12:00 — Carol\n\n"
        "I object — the threat model isn't right.\n\n"
        "### [5] 2026-04-14 13:00 — Dave\n\n"
        "I support this with the caveat that we tighten section 5.\n"
    )


def test_tally_thread_counts_each_bucket() -> None:
    positions, summary = tally_thread(_wglc_thread())
    # 5 messages: 1 chair announcement (no-position), 1 +1, 1 I support,
    # 1 I object, 1 conditional support
    assert summary["support"] == 2
    assert summary["conditional"] == 1
    assert summary["oppose"] == 1
    assert summary["no-position"] == 1
    # And the sender strings have the role suffix stripped.
    names = {p.sender for p in positions}
    assert "Alice" in names
    assert "Alice (Chair)" not in names


def test_tally_thread_preserves_chunk_indices() -> None:
    positions, _summary = tally_thread(_wglc_thread())
    # Chunk indices match section numbers in the file (1-based).
    by_sender = {p.sender: p.chunk_idx for p in positions}
    assert by_sender["Bob"] == 3
    assert by_sender["Carol"] == 4


def test_tally_thread_empty_file() -> None:
    positions, summary = tally_thread("# Empty\n\nNo messages here.\n")
    assert positions == []
    assert summary == {
        "support": 0, "oppose": 0, "conditional": 0, "no-position": 0,
    }


# --- file_supports_tally ---------------------------------------------------


def test_file_supports_tally_thread_files() -> None:
    assert file_supports_tally("threads/2026-04-10-foo.md")


def test_file_supports_tally_issue_files() -> None:
    assert file_supports_tally("issues/org-repo/155.md")


def test_file_supports_tally_rejects_drafts() -> None:
    assert not file_supports_tally("drafts/draft-foo-00.txt")


def test_file_supports_tally_rejects_digests() -> None:
    assert not file_supports_tally("digests/people.md")


# --- MCP tool wiring -------------------------------------------------------


def test_extract_chair_statements_finds_consensus_call(
    isolated_home: Path,
) -> None:
    from ietf_llm.positions import extract_chair_statements
    text = (
        "# WGLC\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice Chen\n\n"
        "I support adoption.\n\n"
        "### [2] 2026-04-11 10:00 — Rifaat Smith\n\n"
        "After reviewing the responses, the chairs conclude that there is "
        "rough consensus to adopt this draft. Closing this thread.\n\n"
        "### [3] 2026-04-12 09:00 — Bob\n\n"
        "Thanks for the call.\n"
    )
    # Rifaat is a chair.
    role_lookup = {"Rifaat Smith": "Chair"}
    statements = extract_chair_statements(text, role_lookup)
    assert len(statements) == 1
    assert statements[0].sender == "Rifaat Smith"
    # The matched phrase is one of the procedural patterns — either
    # `the chairs conclude` or `rough consensus` would qualify; the
    # body carries both. Any decision-language match is correct here.
    matched = statements[0].matched_phrase.lower()
    assert (
        "rough consensus" in matched
        or "conclude" in matched
        or "closing" in matched
    )
    # The excerpt carries the surrounding sentence either way.
    assert "rough consensus to adopt" in statements[0].excerpt.lower()
    # Chunk index is the message number from the section header.
    assert statements[0].chunk_idx == 2


def test_extract_chair_statements_skips_non_chairs(
    isolated_home: Path,
) -> None:
    from ietf_llm.positions import extract_chair_statements
    # Same procedural phrase but from a non-chair → not a statement.
    text = (
        "# T\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Random Participant\n\n"
        "Is there rough consensus on this?\n"
    )
    statements = extract_chair_statements(text, {})
    assert statements == []


def test_extract_chair_statements_skips_chair_without_decision_language(
    isolated_home: Path,
) -> None:
    # Chair posts a technical question without procedural language —
    # not a chair statement. Avoids flooding the section with every
    # chair post on the list.
    from ietf_llm.positions import extract_chair_statements
    text = (
        "# T\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice (Chair)\n\n"
        "What does section 4.2 mean by 'optional'?\n"
    )
    statements = extract_chair_statements(text, {"Alice": "Chair"})
    assert statements == []


def test_tool_tally_positions_renders_summary(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-10-wglc.md", _wglc_thread(),
    )
    out = mcp_server.tool_tally_positions(
        "wg", "threads/2026-04-10-wglc.md",
    )
    # Summary section names every bucket.
    assert "Support: **2**" in out
    assert "Conditional: **1**" in out
    assert "Oppose: **1**" in out
    # Each support / oppose entry surfaces its excerpt.
    assert "I support adoption" in out
    assert "I object" in out
    # And the coverage percentage is reported so the consumer sees
    # what the heuristic missed.
    assert "Coverage:" in out


def test_tool_tally_positions_refuses_drafts(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "drafts/draft-foo-00.txt", "Some draft body.",
    )
    out = mcp_server.tool_tally_positions("wg", "drafts/draft-foo-00.txt")
    # Helpful error pointing at the right file types.
    assert "threads/" in out and "issues/" in out


def test_tool_tally_positions_missing_file(isolated_home: Path) -> None:
    # Build a cache so the WG exists, then ask for a file that isn't there.
    get_wg_file_cache_dir("wg")
    out = mcp_server.tool_tally_positions(
        "wg", "threads/nonexistent.md",
    )
    assert "not found" in out.lower()
