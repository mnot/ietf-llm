"""Tests for the structure-aware search snippet renderer.

The default "first 280 chars" snippet hides the most useful content
in chunks that contain markdown tables (pro/con whiteboards) or
bulleted/numbered lists of options — exactly the content a ranking
LLM needs to see to decide whether a hit is worth opening. These
tests pin down the structure-detection behaviour so it doesn't quietly
regress.
"""

from __future__ import annotations

from ietf_llm.embeddings.snippet import make_snippet


# --- prose fallback (the pre-existing behaviour) --------------------------


def test_prose_only_chunk_falls_back_to_first_chars() -> None:
    text = "Just some prose with no structure.\n\nA second paragraph."
    out = make_snippet(text, max_chars=200)
    # Newlines collapse to single spaces; no [table:] or [list:] prefix.
    assert out.startswith("Just some prose")
    assert "[table" not in out
    assert "[list" not in out


def test_empty_chunk_returns_empty_string() -> None:
    assert make_snippet("") == ""
    assert make_snippet("   \n\n\t") == ""


def test_long_prose_is_truncated_with_marker() -> None:
    # Explicit `[truncated]` marker — consumer feedback: the old
    # ellipsis-only form was ambiguous (could be part of the prose,
    # leaving the consumer unsure whether to re-fetch).
    text = "word " * 200
    out = make_snippet(text, max_chars=50)
    assert len(out) == 50
    assert out.endswith("[truncated]")


def test_prose_collapses_internal_whitespace() -> None:
    text = "First   line.\n\n\n\nSecond    line."
    out = make_snippet(text, max_chars=200)
    # No multi-space runs leak into the snippet.
    assert "  " not in out


# --- table preview (the main consumer feedback) ---------------------------


def test_table_at_start_of_chunk_is_previewed() -> None:
    text = (
        "| Aspect | Pro | Con |\n"
        "|--------|-----|-----|\n"
        "| speed  | fast | n/a |\n"
        "| clarity | clear | terse |\n"
    )
    out = make_snippet(text)
    assert out.startswith("[table: 2 rows × 3 cols]")
    # Header and data rows are surfaced.
    assert "Aspect" in out and "Pro" in out and "Con" in out
    assert "speed" in out
    assert "clarity" in out


def test_table_buried_after_prose_is_still_found() -> None:
    # This is the literal consumer-feedback case: the whiteboard table
    # comes after introductory prose, and the old snippet hid it.
    text = (
        "Lots of introductory prose explaining the situation. " * 5 + "\n\n"
        "| Aspect | Pro | Con |\n"
        "|--------|-----|-----|\n"
        "| speed  | fast | hard |\n"
        "| size   | small | rigid |\n"
        "| ease   | yes | no |\n"
    )
    out = make_snippet(text)
    assert out.startswith("[table: 3 rows × 3 cols]")
    assert "Aspect" in out


def test_table_row_count_reflects_data_rows_not_total_lines() -> None:
    # Five data rows; header + separator are not data.
    rows = "\n".join(f"| r{i} | v{i} |" for i in range(5))
    text = "| col | val |\n|-----|-----|\n" + rows
    out = make_snippet(text)
    assert "[table: 5 rows × 2 cols]" in out


def test_pipe_heavy_prose_without_separator_is_not_a_table() -> None:
    # Three lines that look table-shaped but no `|---|` separator —
    # almost certainly quoted code or a malformed paste, not a table.
    # We must NOT label this `[table: …]`.
    text = (
        "| this looks like a row |\n"
        "| but there's no separator |\n"
        "| so it isn't a real table |\n"
    )
    out = make_snippet(text)
    assert "[table" not in out


def test_table_preview_is_capped_at_max_chars() -> None:
    # Even a huge table must fit in the snippet budget.
    long_row = "| " + (" word" * 30).strip() + " | x |"
    text = (
        "| col1 | col2 |\n"
        "|------|------|\n"
        + "\n".join([long_row] * 10)
    )
    out = make_snippet(text, max_chars=120)
    assert len(out) <= 120
    assert out.startswith("[table:")


# --- list preview ---------------------------------------------------------


def test_bulleted_list_of_three_items_is_previewed() -> None:
    text = "- one\n- two\n- three\n"
    out = make_snippet(text)
    assert out.startswith("[list: 3 items]")
    assert "• one" in out and "• two" in out and "• three" in out


def test_short_list_items_pack_into_full_budget() -> None:
    # 10 short items all fit in the structured budget — greedy packing
    # shows every one rather than wasting space on a "+ 7 more" tail
    # the consumer would have to fetch the chunk to see past.
    text = "\n".join(f"- item {i}" for i in range(10))
    out = make_snippet(text)
    assert out.startswith("[list: 10 items]")
    # Every item appears in the preview.
    for i in range(10):
        assert f"• item {i}" in out
    # No "+ N more" tail when everything fit.
    assert "more" not in out


def test_long_list_truncates_only_what_doesnt_fit() -> None:
    # When items don't all fit, show as many as the budget allows
    # plus a "+ remaining more" tail naming the exact remainder.
    text = "\n".join(f"- this item has a fairly long description {i}" for i in range(20))
    out = make_snippet(text)
    assert out.startswith("[list: 20 items]")
    assert "more" in out
    # The "more" count is the actual remainder, not a hard-coded N-3.
    import re as _re  # local alias to avoid clashing with module-top re
    match = _re.search(r"\+ (\d+) more", out)
    assert match is not None
    shown_count = sum(1 for _ in _re.finditer(r"•", out))
    assert int(match.group(1)) == 20 - shown_count


def test_numbered_list_also_qualifies() -> None:
    text = "1. first\n2. second\n3. third\n4. fourth\n"
    out = make_snippet(text)
    assert out.startswith("[list: 4 items]")


def test_two_bullets_is_not_a_list() -> None:
    # Threshold is 3; two bullets in passing should fall through.
    text = "Some intro.\n\n- first\n- second\n\nMore prose."
    out = make_snippet(text)
    assert "[list" not in out


def test_quoted_bullets_dont_count() -> None:
    # Quoted email replies often look like `> - foo`. Those are
    # someone else's list, not this chunk's, and we mustn't count them.
    text = "Reply text.\n\n> - quoted one\n> - quoted two\n> - quoted three\n"
    out = make_snippet(text)
    assert "[list" not in out


# --- precedence: table beats list, both beat prose ------------------------


def test_table_wins_over_list_when_both_present() -> None:
    text = (
        "- bullet a\n- bullet b\n- bullet c\n\n"
        "| col | val |\n|-----|-----|\n| a | 1 |\n| b | 2 |\n"
    )
    out = make_snippet(text)
    assert out.startswith("[table:")
    assert "[list" not in out


# --- structured snippets get a bigger budget than prose -------------------


def test_structured_content_gets_bigger_default_budget_than_prose() -> None:
    # Consumer feedback: a pro/con table is the actual ranking signal,
    # but the old 280-char cap truncated it mid-bullet. The structured
    # budget must be visibly bigger than the prose one so callers see
    # whole tables / lists.
    from ietf_llm.embeddings.snippet import PROSE_CHARS, STRUCTURED_CHARS
    assert STRUCTURED_CHARS > PROSE_CHARS


def test_long_table_uses_structured_budget_not_prose_one() -> None:
    # A 10-row table previously got chopped at ~280 chars; with the
    # bigger structured budget it should fit a lot more rows.
    rows = "\n".join(f"| Aspect{i} | Pro{i} reasoning | Con{i} reasoning |" for i in range(8))
    text = (
        "Some intro prose here that is short.\n\n"
        "| Aspect | Pro | Con |\n"
        "|--------|-----|-----|\n"
        + rows
    )
    out = make_snippet(text)
    # With prose budget (280) only ~2 data rows would fit. With the
    # structured budget (600) we should see substantially more.
    assert len(out) > 280
    assert out.startswith("[table:")


def test_explicit_max_chars_overrides_default_budget() -> None:
    # Tests that previously asserted the 280-char cap should still
    # work via an explicit override — the override applies to both
    # structured and prose paths.
    text = "| col | val |\n|-----|-----|\n| a | 1 |\n| b | 2 |\n"
    out = make_snippet(text, max_chars=80)
    assert len(out) <= 80
