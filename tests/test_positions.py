"""Tests for the heuristic position extractor (`positions.py`) and
the `tally_positions` MCP tool.

The extractor's contract is high precision over high recall: it should
only return support / oppose when the canonical phrasing is present.
Sentiment-only or technical objections legitimately fall through to
no-position; the renderer surfaces the coverage % so the consumer can
see how much the heuristic missed.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path

from ietf_llm.positions import (
    _SENDER_ROLE_SUFFIX,
    _THREAD_MSG_RE,
    ChairStatement,
    extract_position,
    file_supports_tally,
    render_tally,
    tally_thread,
)
from ietf_llm.utils import get_wg_file_cache_dir

from conftest import write_cache_file


def _sender_of(header_line: str) -> str:
    match = _THREAD_MSG_RE.search(header_line)
    assert match is not None, header_line
    return _SENDER_ROLE_SUFFIX.sub("", match.group(2)).strip()


def test_opener_annotation_does_not_pollute_sender() -> None:
    # An issue file's opener header carries a trailing `_(opened issue)_`
    # annotation; without consuming it, "Alice _(opened issue)_" becomes a
    # distinct identity from "Alice" the commenter, splitting the tally.
    assert _sender_of("### [1] 2026-05-14 — Alice _(opened issue)_") == "Alice"
    assert (
        _sender_of("### [1] 2026-05-14 00:00 — Bob (Chair) _(opened issue)_") == "Bob"
    )
    # The existing reply-to and role-suffix handling still works.
    assert _sender_of("### [3] 2026-05-14 — Carol (reply to [1])") == "Carol"
    assert _sender_of("### [2] 2026-05-14 — Dave (Author)") == "Dave"


# --- extract_position ------------------------------------------------------


def test_bare_plus_one_counts_as_support() -> None:
    label, conf, excerpt, _poll = extract_position("+1\n")
    assert label == "support"
    assert conf == "high"
    assert "+1" in excerpt


def test_bare_minus_one_counts_as_oppose() -> None:
    label, _conf, _excerpt, _poll = extract_position("-1\n")
    assert label == "oppose"


def test_i_support_phrase() -> None:
    label, conf, _excerpt, _poll = extract_position(
        "I support adoption of this draft.\n"
    )
    assert label == "support"
    assert conf == "high"


def test_wglc_review_idioms_count_as_support() -> None:
    # A WG Last Call rarely uses +1/-1; reviewers say "ready to progress",
    # "in good shape", "looks good", "no objection". These register as
    # low-confidence support so a clearly-supportive WGLC thread is not
    # tallied at 0% coverage.
    for body in (
        "I have reviewed the document, and I think it is generally "
        "ready to progress.",
        "I think the document is in a good shape.",
        "This looks good to me.",
        "I have no objection to publishing this.",
    ):
        label, conf, _excerpt, _poll = extract_position(body)
        assert label == "support", body
        assert conf == "low", body


def test_unrelated_prose_is_no_position() -> None:
    label, _conf, _excerpt, _poll = extract_position("The weather is nice today.\n")
    assert label == "no-position"


def test_i_object_phrase() -> None:
    label, _conf, _excerpt, _poll = extract_position(
        "I object to this draft as written.\n"
    )
    assert label == "oppose"


def test_conditional_support_gets_its_own_bucket() -> None:
    label, conf, _excerpt, _poll = extract_position(
        "I support this with the change that we remove section 4.\n"
    )
    assert label == "conditional"
    assert conf == "high"


def test_lgtm_counts_as_support() -> None:
    label, _conf, _excerpt, _poll = extract_position("LGTM\n")
    assert label == "support"


def test_discuss_counts_as_oppose() -> None:
    # IESG ballot position. Treated as opposition for tally purposes —
    # a DISCUSS holds publication.
    label, _conf, _excerpt, _poll = extract_position(
        "DISCUSS\n\nThis draft has...\n"
    )
    assert label == "oppose"


def test_quoted_plus_one_does_not_count() -> None:
    # The author is quoting someone else's +1, then disagreeing.
    body = "> +1 to publishing as-is\n\nI object — premature.\n"
    label, _conf, _excerpt, _poll = extract_position(body)
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
    label, _conf, _excerpt, _poll = extract_position(body)
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
    label, _conf, _excerpt, _poll = extract_position(body)
    assert label == "no-position"


def test_no_position_message_returns_empty_excerpt() -> None:
    _label, conf, excerpt, _poll = extract_position(
        "Just a clarifying question.\n"
    )
    assert conf == ""
    assert excerpt == ""


# --- poll-syntax detection ------------------------------------------------


def test_option_n_at_line_start_is_poll_choice() -> None:
    label, _conf, _excerpt, choice = extract_position("Option 2\n")
    assert label == "poll"
    assert choice == "2"


def test_hash_n_line_is_poll_choice() -> None:
    label, _conf, _excerpt, choice = extract_position("#3\n")
    assert label == "poll"
    assert choice == "3"


def test_i_prefer_n_is_poll_choice() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "I prefer 2 because it's simpler.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_my_vote_is_poll_choice() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "My vote is option B.\n"
    )
    assert label == "poll"
    assert choice == "B"


def test_poll_choice_preferred_over_support_when_both_match() -> None:
    # The order of precedence puts poll detection BEFORE strong
    # support, so "I prefer 2" doesn't bucket as support.
    label, _conf, _excerpt, choice = extract_position(
        "I prefer option 2.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_long_token_after_prefer_is_not_a_poll_choice() -> None:
    # "I prefer option" only looks like a poll choice if followed by a
    # short token. "I prefer brevity" must not register as choice
    # "BREVITY".
    label, _conf, _excerpt, choice = extract_position(
        "I prefer brevity in this discussion.\n"
    )
    assert label != "poll"
    assert choice is None


# --- consumer-reported misses (regression set) ---------------------------
#
# Each of these phrasings was missed by the earlier single-regex poll
# detector. The two-stage intent-plus-marker design catches them.


def test_i_want_option_n() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "I want option 2 because the threat model is cleaner.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_my_preference_would_be_option_n() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "My preference would be option 2.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_strong_preference_for_hash_n() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "Strong preference for #2.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_i_think_the_answer_is_hash_n() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "I think the answer is #1.\n"
    )
    assert label == "poll"
    assert choice == "1"


def test_definitely_with_parenthesised_hash() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "Definitely anonymous first (#1), then attested later.\n"
    )
    assert label == "poll"
    assert choice == "1"


def test_plus_one_on_hash_n_is_poll_not_bare_support() -> None:
    # Previously matched "+1" as bare strong support and missed the
    # "#1" — losing the option signal. With intent + marker, the
    # poll detection wins because it runs first AND finds the marker.
    label, _conf, _excerpt, choice = extract_position(
        "+1 on #1, focusing first on the anonymous approach.\n"
    )
    assert label == "poll"
    assert choice == "1"


def test_in_favor_of_option_n() -> None:
    # Previously misclassified as weak support; the marker was right
    # there but only the strong-support / weak-support regexes saw it.
    label, _conf, _excerpt, choice = extract_position(
        "After thinking about this, I am in favor of option 2.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_preference_is_hash_n_does_not_yield_choice_is() -> None:
    # The original parse bug: optional `(?:\s+is)?` allowed the
    # engine to capture "IS" as the choice. The current detector
    # builds the choice from a structurally-anchored marker capture
    # (`#1`), so "is" can't sneak in.
    label, _conf, _excerpt, choice = extract_position(
        "My preference is #1.\n"
    )
    assert label == "poll"
    assert choice == "1"
    assert choice != "IS"


def test_id_apostrophe_form_is_poll_choice() -> None:
    # "I'd prefer #2" (no space between I and 'd) was missing from
    # the earlier regex — fixed by handling the contraction
    # explicitly.
    label, _conf, _excerpt, choice = extract_position("I'd prefer #2.\n")
    assert label == "poll"
    assert choice == "2"


def test_id_like_form_is_poll_choice() -> None:
    label, _conf, _excerpt, choice = extract_position(
        "I'd like option 2.\n"
    )
    assert label == "poll"
    assert choice == "2"


def test_marker_without_intent_is_not_a_vote() -> None:
    # A message that mentions an option in prose but doesn't register
    # an opinion must not be classified as a vote. Avoids
    # false-positives on chair questions / meeting summaries / etc.
    body = (
        "I see we discussed option 1 and option 2 in the last "
        "meeting, but no decision was made.\n"
    )
    label, _conf, _excerpt, choice = extract_position(body)
    assert label != "poll"
    assert choice is None


def test_intent_far_from_marker_is_not_a_vote() -> None:
    # Intent and marker must be near each other; an "I support" at
    # the top of a long message and an "option 2" 500 chars later
    # shouldn't bind into a poll vote.
    body = (
        "I support keeping the WG charter as-is.\n\n"
        + ("Some paragraph of unrelated discussion. " * 8)
        + "We could also consider option 2 in a follow-up draft.\n"
    )
    label, _conf, _excerpt, _choice = extract_position(body)
    # Should fall through to support (the "I support" intent at the top).
    assert label == "support"


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


def _prose_thread(n: int) -> str:
    # A thread where every message argues in prose with no canonical position
    # token — the ethics-04 shape: full of opinion, zero keyword coverage.
    lines = ["# Adoption debate\n", f"**Messages:** {n}\n", "## Messages\n"]
    prose = [
        "The scope here is far too broad for our charter.",
        "The threat model in section 3 does not match deployment reality.",
        "Sixty days have elapsed and nothing has converged on this.",
        "Routing this to the meeting seems premature given the open issues.",
        "As written, the document raises more questions than it answers.",
        "This needs much more analysis before it can go anywhere.",
    ]
    for i in range(n):
        lines.append(f"### [{i + 1}] 2026-06-1{i} 09:00 — Person{i}\n")
        lines.append(f"{prose[i % len(prose)]}\n")
    return "\n".join(lines)


def _mixed_low_coverage_thread() -> str:
    # 10 messages: nine prose (no-position) + one canonical support — 10%
    # coverage, low but non-zero, so the per-side sections do render.
    lines = ["# Mixed\n", "**Messages:** 10\n", "## Messages\n"]
    for i in range(9):
        lines.append(f"### [{i + 1}] 2026-06-{i + 1:02d} 09:00 — Person{i}\n")
        lines.append("The scope here seems too broad for our charter.\n")
    lines.append("### [10] 2026-06-10 09:00 — Late Supporter\n")
    lines.append("I support adoption of this draft.\n")
    return "\n".join(lines)


def test_render_tally_partial_low_coverage_no_count_leak() -> None:
    # At low-but-non-zero coverage the summary withholds the aggregate, so the
    # section / by-author headers must not leak a quotable count either — but
    # the grounded per-author row still renders.
    positions, summary = tally_thread(_mixed_low_coverage_thread())
    assert summary["support"] == 1
    assert summary["no-position"] == 9
    out = render_tally("threads/mixed.md", positions, summary)
    assert "Low coverage (10%)" in out
    assert "withheld" in out
    assert "I support adoption of this draft." in out  # grounded row survives
    assert "Late Supporter" in out
    assert "Support: **1**" not in out  # summary withheld
    assert "## Support (1)" not in out  # header count withheld
    assert "## By author (1)" not in out
    assert "## Support" in out  # the section itself still renders


def test_coverage_banner_chair_clause_gated_on_presence() -> None:
    positions, summary = tally_thread(_prose_thread(6))
    # No chair statements: the banner must not name a section that won't render.
    out_none = render_tally("threads/a.md", positions, summary)
    assert "Chair statements** section" not in out_none
    assert "read the messages at the chunk indices" in out_none
    # With chair statements: the banner names the section.
    cs = [
        ChairStatement(
            sender="Chair P",
            chunk_idx=2,
            excerpt="we will route this to the meeting",
            matched_phrase="adoption call",
            role="Chair",
        )
    ]
    out_cs = render_tally("threads/a.md", positions, summary, chair_statements=cs)
    assert "Chair statements** section" in out_cs


def test_render_tally_withholds_counts_at_low_coverage() -> None:
    positions, summary = tally_thread(_prose_thread(6))
    assert summary["no-position"] == 6  # all prose, nothing classified
    out = render_tally("threads/adoption.md", positions, summary)
    # Prominent banner up front, counts withheld, the 0 never rendered.
    assert "classified none of the 6 messages" in out
    assert "withheld" in out
    assert "Support: **0**" not in out
    assert "Oppose: **0**" not in out
    # Coverage still reported, and the navigable chunk index survives.
    assert "Coverage: 0%" in out
    assert "No detectable position" in out
    assert "[chunks" in out


def test_render_tally_keeps_counts_at_high_coverage() -> None:
    positions, summary = tally_thread(_wglc_thread())  # 80% coverage
    out = render_tally("threads/wglc.md", positions, summary)
    assert "Support: **2**" in out
    assert "Oppose: **1**" in out
    assert "withheld" not in out
    assert "Low coverage" not in out and "classified none" not in out


def test_render_tally_small_thread_does_not_trigger_banner() -> None:
    # Below the min-message floor, a 0% tally is noise, not a signal — show
    # the (zero) counts plainly rather than a scary withheld banner.
    positions, summary = tally_thread(_prose_thread(3))
    out = render_tally("threads/small.md", positions, summary)
    assert "withheld" not in out
    assert "classified none" not in out
    assert "Support: **0**" in out


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
        "support": 0, "oppose": 0, "conditional": 0,
        "poll": 0, "no-position": 0,
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
    out = mcp.topic.tool_tally_positions(
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
    out = mcp.topic.tool_tally_positions("wg", "drafts/draft-foo-00.txt")
    # Helpful error pointing at the right file types.
    assert "threads/" in out and "issues/" in out


def test_tool_tally_positions_missing_file(isolated_home: Path) -> None:
    # Build a cache so the WG exists, then ask for a file that isn't there.
    get_wg_file_cache_dir("wg")
    out = mcp.topic.tool_tally_positions(
        "wg", "threads/nonexistent.md",
    )
    assert "not found" in out.lower()
