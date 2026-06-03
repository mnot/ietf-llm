"""Tests for the overview tool.

build_overview reads the existing digest .md files and composes a
tight one-call summary. The tests construct minimal synthetic
digests in a tmp cache dir so we don't need a real corpus.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest.overview import _charter_excerpt, build_overview


def _seed_digests(cache: Path, *, with_authors: bool = True) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "digests").mkdir(exist_ok=True)
    (cache / "digests/people.md").write_text(
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
    (cache / "digests/issues.md").write_text(
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
    (cache / "digests/threads.md").write_text(
        "# wg: threads\n\n"
        "| Subject | Msgs | Participants | First | Last | File |\n"
        "|---------|------|--------------|-------|------|------|\n"
        "| Newest | 3 | 2 | 2026-05-20 | 2026-05-24 | `t1.md` |\n"
        "| Middle | 5 | 3 | 2026-04-01 | 2026-05-15 | `t2.md` |\n"
        "| Old | 1 | 1 | 2025-01-01 | 2025-01-01 | `t3.md` |\n"
    )
    (cache / "digests/timeline.md").write_text(
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
    assert "## Internet-Drafts" in out
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


def test_overview_active_threads_excludes_single_message(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    # The thread section is heat-ranked: the two multi-message threads
    # appear; the single-message "Old" thread has no back-and-forth and is
    # excluded.
    assert "## Most active threads" in out
    sec = out.split("## Most active threads", 1)[1].split("\n## ", 1)[0]
    assert "Newest" in sec and "Middle" in sec
    assert "Old" not in sec


def test_overview_includes_latest_events(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "## Recent activity" in out
    assert "IETF 125 meeting" in out
    assert "draft-ietf-wg-vocab-06" in out


def test_overview_recent_activity_folds_mechanical_events(tmp_path: Path) -> None:
    # Discussion / decision events (WGLC, adoption, meeting) are shown as
    # lines; the two mechanical classes — automated I-D Action publications
    # (`… published · threads/…`) and per-AD IESG ballot positions
    # (`… → …`) — are folded into counts so they do not crowd the list.
    _seed_digests(tmp_path)
    (tmp_path / "digests/timeline.md").write_text(
        "# wg: timeline\n\n"
        "## 2026\n\n"
        "- **2026-05-20** — WGLC for `draft-ietf-wg-foo` started\n"
        "- **2026-05-18** — `draft-ietf-wg-foo`: Roman Danyliw → DISCUSS · ballots/x\n"
        "- **2026-05-17** — `draft-ietf-wg-foo`: Alice → No Objection · ballots/x\n"
        "- **2026-05-10** — adoption call for `draft-ietf-wg-bar`\n"
        "- **2026-04-15** — `draft-ietf-wg-foo-03` published · `threads/t.md`\n"
        "- **2026-03-16** — IETF 125 meeting held\n"
    )
    out = build_overview("wg", str(tmp_path))
    activity = out.split("## Recent activity", 1)[1].split("## ", 1)[0]
    # Discussion events shown as lines.
    assert "WGLC" in activity
    assert "adoption call" in activity
    assert "IETF 125" in activity
    # Mechanical events folded into counts, not shown as lines.
    assert "→ DISCUSS" not in activity
    assert "No Objection" not in activity
    assert "1 I-D Action draft publication(s)" in activity
    assert "2 IESG ballot position(s)" in activity


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
    (cache / "digests").mkdir(exist_ok=True)
    (cache / "digests/issues.md").write_text(
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
    # Different WG name → different copy-pasteable calls. The cache
    # layout is the same; only the rendered WG name differs.
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


def test_overview_splits_drafts_from_rfcs(tmp_path: Path) -> None:
    # Documents partition into an Internet-Drafts section (full bullets)
    # and a compact Published RFCs section. Cited RFCs surface inline with
    # their count; uncited RFCs collapse to a count + pointer.
    _seed_digests(tmp_path, with_authors=False)
    (tmp_path / "digests/people.md").write_text(
        "# wg: participants\n\n"
        "## Document authors / editors (1)\n\n"
        "| Name | Documents | Email |\n|---|---|---|\n"
        "| Jane Doe | draft-ietf-wg-live, rfc9110, rfc9111, rfc7230 | jane@x |\n"
    )
    (tmp_path / "digests/citations.md").write_text(
        "# wg: citations\n\n"
        "## `rfc9110` (12 citations)\n\n"
        "- thread/a.md\n"
    )
    out = build_overview("wg", str(tmp_path))

    # Drafts section has the draft, not the RFCs.
    assert "## Internet-Drafts (1 active)" in out
    assert "`draft-ietf-wg-live`" in out
    # RFCs section: 3 total, the cited one inline, the other two collapsed.
    assert "## Published RFCs (3)" in out
    assert "`rfc9110` _(cited in 12)_" in out
    assert "2 more in `drafts/`" in out
    # An uncited RFC is NOT given its own bullet line.
    assert "`rfc7230` —" not in out
    assert "`rfc9111` —" not in out


def test_draft_concluded_by_expiry() -> None:
    import datetime
    from ietf_llm.digest.overview import _draft_concluded

    now = datetime.datetime(2026, 5, 30, tzinfo=datetime.timezone.utc)
    exp = {
        "draft-old": {"expires": "2014-08-10T00:00:00Z", "state": "rfc"},  # past
        "draft-live": {"expires": "2026-11-14T00:00:00Z", "state": "active"},  # future
        "draft-bad": {"expires": "not-a-date", "state": None},  # unparseable → active
        "draft-nostate": {"expires": "", "state": "repl"},  # no expiry → active here
    }
    assert _draft_concluded("draft-old", exp, now) is True
    assert _draft_concluded("draft-live", exp, now) is False
    assert _draft_concluded("draft-bad", exp, now) is False
    assert _draft_concluded("draft-nostate", exp, now) is False  # expiry-driven only
    assert _draft_concluded("draft-unknown", exp, now) is False  # not in manifest


def test_overview_splits_active_from_concluded_drafts(
    tmp_path: Path, monkeypatch: object,
) -> None:
    # A WG draft with a past expiry collapses into the concluded count;
    # the future-expiry one stays an active bullet.
    from ietf_llm.digest import overview as overview_mod

    _seed_digests(tmp_path, with_authors=False)
    (tmp_path / "digests/people.md").write_text(
        "# wg: participants\n\n"
        "## Document authors / editors (1)\n\n"
        "| Name | Documents | Email |\n|---|---|---|\n"
        "| Jane Doe | draft-ietf-wg-live, draft-ietf-wg-old | jane@x |\n"
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        overview_mod,
        "load_documents_manifest",
        lambda _wg: {
            "draft-ietf-wg-live": {"expires": "2099-01-01T00:00:00Z", "state": "active"},
            "draft-ietf-wg-old": {"expires": "2014-01-01T00:00:00Z", "state": "rfc"},
        },
    )
    out = build_overview("wg", str(tmp_path))
    assert "## Internet-Drafts (1 active)" in out
    assert "`draft-ietf-wg-live`" in out
    assert "1 expired or concluded draft(s), in `drafts/`" in out
    # The concluded draft is not given its own active bullet.
    assert "`draft-ietf-wg-old` —" not in out


def test_overview_flags_drafts_blocked_on_discuss(tmp_path: Path) -> None:
    # A draft with an unresolved IESG DISCUSS (from the ballot tally) is
    # flagged with a pointer to the ballot, so a "what is going on" answer
    # is nudged to read the substance, not just report the structure.
    _seed_digests(tmp_path)
    (tmp_path / "ballots").mkdir()
    (tmp_path / "ballots" / "draft-ietf-wg-foo.md").write_text(
        "# IESG ballot: draft-ietf-wg-foo\n\n"
        "**Tally:** 1 DISCUSS, 2 Yes, 5 No Objection\n"
    )
    (tmp_path / "ballots" / "draft-ietf-wg-clear.md").write_text(
        "# IESG ballot: draft-ietf-wg-clear\n\n**Tally:** 3 Yes, 10 No Objection\n"
    )
    out = build_overview("wg", str(tmp_path))
    assert "## ⚠ Blocked on IESG DISCUSS (1)" in out
    assert "`draft-ietf-wg-foo`" in out
    assert "ballots/draft-ietf-wg-foo.md" in out
    # A draft with no DISCUSS in its tally is not flagged.
    assert "draft-ietf-wg-clear" not in out


def test_overview_surfaces_most_active_threads(tmp_path: Path) -> None:
    # Ranked by message count within a recent window: the heat signal.
    # Single-message threads and threads outside the window are excluded.
    _seed_digests(tmp_path)
    (tmp_path / "digests/threads.md").write_text(
        "# wg: threads\n\n"
        "| Subject | Msgs | Participants | First | Last | File |\n"
        "|---|---|---|---|---|---|\n"
        "| Hot debate | 12 | 5 | 2026-05-01 | 2026-05-20 | t1.md |\n"
        "| Mild chat | 3 | 2 | 2026-05-02 | 2026-05-18 | t2.md |\n"
        "| Solo announce | 1 | 1 | 2026-05-19 | 2026-05-19 | t3.md |\n"
        "| Old big thread | 40 | 9 | 2024-01-01 | 2024-02-01 | t4.md |\n"
    )
    out = build_overview("wg", str(tmp_path))
    sec = out.split("## Most active threads", 1)[1].split("\n## ", 1)[0]
    assert "Hot debate" in sec
    assert "Mild chat" in sec
    assert "Solo announce" not in sec  # single message: no back-and-forth
    assert "Old big thread" not in sec  # outside the recency window
    assert sec.index("Hot debate") < sec.index("Mild chat")  # ranked by msgs


def test_overview_no_blocked_section_without_discuss(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    out = build_overview("wg", str(tmp_path))
    assert "Blocked on IESG DISCUSS" not in out


def test_overview_explains_citation_counts_when_present(tmp_path: Path) -> None:
    # When a draft carries a "cited in N" tag, the overview includes a
    # one-line legend defining the figure. Absent any citations, no legend.
    _seed_digests(tmp_path)
    out_no_cites = build_overview("wg", str(tmp_path))
    assert '"cited in N"' not in out_no_cites

    (tmp_path / "digests/citations.md").write_text(
        "# wg: citations\n\n## `draft-ietf-wg-vocab` (4 citations)\n\n- t.md\n"
    )
    out = build_overview("wg", str(tmp_path))
    assert "cited in 4" in out  # the count on the draft bullet
    assert '"cited in N"' in out  # the explaining legend
    assert "not weighted by recency" in out


def test_overview_works_without_document_authors_section(tmp_path: Path) -> None:
    _seed_digests(tmp_path, with_authors=False)
    out = build_overview("wg", str(tmp_path))
    # Leadership still surfaces, documents sections absent.
    assert "**Chairs:**" in out
    assert "## Internet-Drafts" not in out
    assert "## Published RFCs" not in out


# --- _subject_prefix_frequencies ------------------------------------------


def test_subject_prefix_frequencies_counts_bracketed_tokens(
    tmp_path: Path,
) -> None:
    # Build a synthetic threads/ dir with messages carrying TLS-style
    # subject prefixes; the helper should report the frequency table.
    from ietf_llm.digest.overview import _subject_prefix_frequencies  # pylint: disable=import-outside-toplevel
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / "2026-04-10-foo.md").write_text(
        "# Foo\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\n"
        "_Subject:_ [TLS] [mlkem] consensus call\n\n"
        "body\n\n"
        "### [2] 2026-04-11 10:00 — Bob\n\n"
        "_Subject:_ Re: [TLS] [mlkem] consensus call\n\n"
        "body\n"
    )
    (threads_dir / "2026-04-12-bar.md").write_text(
        "# Bar\n\n## Messages\n\n"
        "### [1] 2026-04-12 09:00 — Carol\n\n"
        "_Subject:_ [TLS] [ech] negotiation\n\n"
        "body\n"
    )
    freqs = _subject_prefix_frequencies(str(tmp_path))
    by_prefix = dict(freqs)
    # [tls] appears 3 times (every subject); [mlkem] twice; [ech] once.
    assert by_prefix.get("[tls]") == 3
    assert by_prefix.get("[mlkem]") == 2
    assert by_prefix.get("[ech]") == 1


def test_subject_prefix_frequencies_strips_re_fwd(tmp_path: Path) -> None:
    # `Re: ` / `Fwd:` chrome before the first bracketed prefix must be
    # stripped so the prefix is still recognised.
    from ietf_llm.digest.overview import _subject_prefix_frequencies  # pylint: disable=import-outside-toplevel
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    (threads_dir / "x.md").write_text(
        "# x\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\n"
        "_Subject:_ Re: Re: Fwd: [mlkem] something\n\n"
        "body\n"
    )
    freqs = _subject_prefix_frequencies(str(tmp_path))
    assert ("[mlkem]", 1) in freqs


def test_subject_prefix_frequencies_empty_when_no_threads(
    tmp_path: Path,
) -> None:
    from ietf_llm.digest.overview import _subject_prefix_frequencies  # pylint: disable=import-outside-toplevel
    # No threads/ dir at all → empty list, not crash.
    assert _subject_prefix_frequencies(str(tmp_path)) == []


def test_overview_links_charter_when_present(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    # Charter body needs to be a real paragraph (>= 80 chars) so the
    # excerpt extractor recognises it as the mission statement rather
    # than a procedural header.
    (tmp_path / "charter.txt").write_text(
        "The Working Group develops protocols for transferring "
        "structured data between AI agents and origin servers, with "
        "attention to authentication, scope of use, and privacy of "
        "end-user signals. Out of scope: payment flows."
    )
    out = build_overview("wg", str(tmp_path))
    assert "charter.txt" in out
    assert "Charter" in out
    # The excerpt body is inlined as a blockquote.
    assert "Out of scope" in out


def test_charter_excerpt_skips_writer_header(tmp_path: Path) -> None:
    # Regression: our charter writer prepends a `Working Group Charter:`
    # / `Source:` / `===` rule block with no blank line between the
    # lines, and the rule alone is 80 chars — so the header used to clear
    # the length gate and be returned as the excerpt. The excerpt must be
    # the mission paragraph instead.
    header = (
        "Working Group Charter: httpbis\n"
        "Source: https://www.ietf.org/charter/charter-ietf-httpbis-09.txt\n"
        + ("=" * 80)
    )
    mission = (
        "Hypertext Transfer Protocol (HTTP) is an Internet Standard "
        "defined in STD 97 / RFC 9110, used across multiple transport "
        "versions and maintained by this working group."
    )
    (tmp_path / "charter.txt").write_text(header + "\n\n" + mission + "\n")
    excerpt = _charter_excerpt(str(tmp_path))
    assert excerpt is not None
    assert excerpt.startswith("Hypertext Transfer Protocol")
    assert "Working Group Charter:" not in excerpt
    assert "Source:" not in excerpt
    assert "===" not in excerpt


def test_overview_omits_charter_line_when_file_missing(tmp_path: Path) -> None:
    _seed_digests(tmp_path)
    # No charter.txt written; overview should not mention it.
    out = build_overview("wg", str(tmp_path))
    assert "charter.txt" not in out


# Freshness is no longer self-reported by build_overview; it is
# prepended by the MCP layer's _with_freshness on every top-level
# response. See test_freshness.py (freshness_line) and test_mcp_server.py.
