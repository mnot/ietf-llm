"""Tests for the overview tool.

build_overview reads the existing digest .md files and composes a
tight one-call summary. The tests construct minimal synthetic
digests in a tmp cache dir so we don't need a real corpus.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest.overview import build_overview


def _seed_digests(cache: Path, *, with_authors: bool = True) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "wg-_people.md").write_text(
        "# wg: participants\n\n"
        "_Preamble._\n\n"
        "## Working Group leadership (3)\n\n"
        "| Role | Name | Email |\n|---|---|---|\n"
        "| Chair | Mark Nottingham | mnot@x |\n"
        "| Chair | Suresh Krishnan | suresh@x |\n"
        "| Area Director | Mike Bishop | mb@x |\n\n"
        + (
            "## Document authors / editors (2)\n\n"
            "| Name | Documents | Email |\n|---|---|---|\n"
            "| Paul Keller | draft-ietf-wg-vocab | paul@x |\n"
            "| Martin Thomson | draft-ietf-wg-vocab (ed.), draft-ietf-wg-attach | mt@x |\n\n"
            if with_authors
            else ""
        )
    )
    (cache / "wg-_issues.md").write_text(
        "# wg: issues\n\n"
        "## org/repo\n\n"
        "| # | State | Title | Labels | Comments | Updated | Author |\n"
        "|---|-------|-------|--------|----------|---------|--------|\n"
        "| 1 | OPEN | First open | x | 2 | 2026-05-14 | Mark |\n"
        "| 2 | OPEN | Second open | y | 0 | 2026-05-13 | Bob |\n"
        "| 3 | CLOSED | Closed thing | z | 1 | 2026-05-12 | Carol |\n"
        "| 4 | OPEN | Third open | a | 3 | 2026-05-11 | Dave |\n"
        "| 5 | OPEN | Fourth open | b | 4 | 2026-05-10 | Eve |\n"
        "| 6 | OPEN | Fifth open | c | 5 | 2026-05-09 | Faye |\n"
        "| 7 | OPEN | Sixth open | d | 6 | 2026-05-08 | Greg |\n"
    )
    (cache / "wg-_threads.md").write_text(
        "# wg: threads\n\n"
        "| Subject | Msgs | Participants | First | Last | File |\n"
        "|---------|------|--------------|-------|------|------|\n"
        "| Newest | 3 | 2 | 2026-05-20 | 2026-05-24 | `t1.md` |\n"
        "| Middle | 5 | 3 | 2026-04-01 | 2026-05-15 | `t2.md` |\n"
        "| Old | 1 | 1 | 2025-01-01 | 2025-01-01 | `t3.md` |\n"
    )
    (cache / "wg-_timeline.md").write_text(
        "# wg: timeline\n\n"
        "## 2026\n\n"
        "- **2026-04-27** — `draft-ietf-wg-vocab-06` published\n"
        "- **2026-03-16** — IETF 125 meeting held\n"
    )


def test_overview_includes_leadership(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "**Chairs:** Mark Nottingham, Suresh Krishnan" in out
    assert "**AD:** Mike Bishop" in out


def test_overview_includes_documents(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "## Documents" in out
    assert "`draft-ietf-wg-vocab`" in out
    assert "Martin Thomson" in out
    assert "(ed.)" in out


def test_overview_caps_open_issues_at_five(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # 5 of the 6 open issues should appear; closed one excluded.
    for n in ("First open", "Second open", "Third open", "Fourth open", "Fifth open"):
        assert n in out
    assert "Closed thing" not in out
    assert "Sixth open" not in out  # 6th of 6 open is dropped at limit=5


def test_overview_caps_threads_at_five(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # Only 3 threads exist; all should appear, no "5 most recent" issue.
    assert "Newest" in out and "Middle" in out and "Old" in out


def test_overview_includes_latest_events(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "IETF 125 meeting" in out
    assert "draft-ietf-wg-vocab-06" in out


def test_overview_includes_call_pattern_hints(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # The trailing pointer to read_digest + search_corpus should be there;
    # this is what teaches the agent how to drill deeper without burning
    # context.
    assert "read_digest" in out
    assert "search_corpus" in out


def test_overview_footer_keys_calls_to_question_shape(tmp_path: Path) -> None:
    # Consumer feedback: the old footer ("for depth, call …") didn't
    # tell the agent WHICH tool to use for WHICH question. The new
    # footer keys each suggested call to a question shape — label=
    # for topical, state="closed" for decisions, etc.
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # Question-shape headings are present.
    assert "Where to look next" in out
    assert "question shape" in out
    # And the call signatures for the high-value moves are concrete
    # (with the WG name baked in, so the agent can copy-paste).
    assert 'search_corpus("wg", "X", label=' in out
    assert 'search_corpus("wg", "X", state="closed")' in out
    assert 'read_digest("wg"' in out


def test_overview_lists_top_labels_with_counts(tmp_path: Path) -> None:
    # The issues digest's seed has one label per row: x, y, z, a, b, c, d.
    # Each appears once, so the Top labels section should list them all
    # with count 1 — proving the frequency aggregator works even when
    # there are no ties.
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "Top issue labels" in out
    # Each label is rendered with its count in backticks.
    assert "`x` (1)" in out
    assert "`y` (1)" in out


def test_overview_top_labels_aggregates_repeats(tmp_path: Path) -> None:
    # Seed issues with a label that appears multiple times so the
    # count is non-trivial.
    cache = tmp_path
    (cache / "wg-_issues.md").write_text(
        "# wg: issues\n\n"
        "## org/repo\n\n"
        "| # | State | Title | Labels | Comments | Updated | Author |\n"
        "|---|-------|-------|--------|----------|---------|--------|\n"
        "| 1 | OPEN | A | top-level | 1 | 2026-05-14 | Mark |\n"
        "| 2 | OPEN | B | top-level, vocab | 1 | 2026-05-13 | Mark |\n"
        "| 3 | OPEN | C | vocab | 1 | 2026-05-12 | Mark |\n"
    )
    out = build_overview("wg", str(cache))
    # top-level appears twice, vocab twice, so both should show "(2)".
    assert "`top-level` (2)" in out
    assert "`vocab` (2)" in out


def test_overview_footer_baking_in_wg_name(tmp_path: Path) -> None:
    # Different WG name should produce different copy-pasteable calls.
    _seed_digests(tmp_path)
    # Reuse the same digest files by passing a WG name whose digest
    # paths happen to exist… simplest: write digests under the alias.
    (tmp_path / "other-_people.md").write_text(
        (tmp_path / "wg-_people.md").read_text()
    )
    out = build_overview("other", str(tmp_path))
    assert 'search_corpus("other"' in out


def test_overview_handles_missing_cache(tmp_path: Path) -> None:
    out = build_overview("wg", str(tmp_path / "nope"))
    assert "No cache" in out


def test_overview_size_is_modest(tmp_path: Path) -> None:
    """Sanity check on the headline claim: ~30 lines, not the full digests."""
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # Realistic upper bound for a small synthetic corpus.
    assert len(out.splitlines()) < 50
    assert len(out) < 4000


def test_overview_works_without_document_authors_section(tmp_path: Path) -> None:
    _seed_digests(tmp_path, with_authors=False)
    out = build_overview("wg", str(tmp_path))
    # Leadership still surfaces, documents section absent.
    assert "**Chairs:**" in out
    assert "## Documents" not in out
