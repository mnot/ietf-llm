"""Tests for grep_corpus — the literal/lexical search over the on-disk cache.

The point of the tool is that a *zero* is quotable: semantic non-retrieval is
weak evidence of absence, a complete line scan is not. So most of what is
asserted here is about the honesty of the output — that the denominator is
reported, that a cap is announced rather than silently applied, and that a
scoped scan says it was scoped — not just that matching lines come back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.search import build_index
from ietf_llm.log import Verbosity
from ietf_llm.mcp import grep
from ietf_llm.mcp.grep import _MAX_GREP_LIMIT, tool_grep_corpus
from ietf_llm.paths import get_wg_file_cache_dir

from conftest import write_cache_file


class _StubModel:
    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _build_with_stub(wg: str) -> None:
    cache = get_wg_file_cache_dir(wg)
    embeddings._MODEL_CACHE["stub"] = _StubModel()
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


def _seed(home: Path) -> None:
    write_cache_file(
        home,
        "wg",
        "threads/2026-04-10-mlkem.md",
        (
            "# MLKEM\n\n## Messages\n\n"
            "### [1] 2026-04-10 09:00 — Alice\n\n"
            "_Subject:_ MLKEM\n\n"
            "We should fail closed here, per RFC 8890.\n\n"
            "### [2] 2026-04-11 09:00 — Bob\n\n"
            "_Subject:_ Re: MLKEM\n\n"
            "Agreed, FAIL CLOSED is the safe default.\n"
        ),
    )
    write_cache_file(
        home,
        "wg",
        "drafts/draft-example-thing-00.txt",
        "An older revision mentioning the widget parameter.\n",
    )


# --- matching ----------------------------------------------------------------


def test_literal_match_reports_file_line_and_count(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail closed")
    # Case-insensitive by default, so both spellings match.
    assert "2 matching line(s) in 1 file(s)" in out
    assert "threads/2026-04-10-mlkem.md" in out
    assert "fail closed here" in out
    assert "FAIL CLOSED is the safe default" in out


def test_case_sensitive_narrows_the_match(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "FAIL CLOSED", case_sensitive=True)
    assert "1 matching line(s)" in out
    assert "safe default" in out


def test_regex_mode(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", r"RFC ?8890", regex=True)
    assert "1 matching line(s)" in out
    # The same pattern as a literal finds nothing — proving regex was honoured
    # rather than the escape path being taken silently.
    assert "no matches" in tool_grep_corpus("wg", r"RFC ?8890")


def test_anchored_regex_matches_per_line_not_per_file(isolated_home: Path) -> None:
    """`_scan_file` prefilters over the file's whole text, so without
    re.MULTILINE a `^`-anchored pattern would fail the prefilter, skip the file
    entirely, and report a confidently-worded zero — the unsound negative this
    tool exists to prevent."""
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "^_Subject:", regex=True)
    assert "no matches" not in out
    assert "2 matching line(s)" in out
    # `$` anchors per line too.
    assert "no matches" not in tool_grep_corpus("wg", r"per RFC 8890\.$", regex=True)
    # And an anchor that genuinely cannot match still reports absence.
    assert "no matches" in tool_grep_corpus("wg", "^zzznope", regex=True)


def test_invalid_regex_is_reported_not_raised(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail (closed", regex=True)
    assert "Invalid regex" in out
    assert "drop `regex=True`" in out


def test_empty_pattern_is_refused(isolated_home: Path) -> None:
    _seed(isolated_home)
    assert "needs a non-empty" in tool_grep_corpus("wg", "   ")


# --- the negative claim ------------------------------------------------------


def test_no_match_states_the_denominator_and_its_limits(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "quantum annealing")
    assert "no matches" in out
    # The denominator: how many files this conclusion rests on.
    assert "scanned 2 file(s)" in out
    # And the three things that still bound it, so the caller cannot read the
    # zero as broader than it is.
    assert "gather window" in out
    assert "corpus boundary" in out
    assert "single line" in out


def _seed_github(home: Path) -> None:
    """An issue file plus the archive it came from, so the record has both
    something to scan and a stated ceiling."""
    write_cache_file(home, "wg", "issues/org-repo/12.md", "# Widget handling\n")
    write_cache_file(
        home,
        "wg",
        "github/org-repo.json",
        '{"repo": "org/repo", "timestamp": "2026-08-06T01:49:14Z", '
        '"issues": [{"number": 12}], "pulls": [{"number": 14}]}',
    )


def test_no_match_over_github_states_where_the_record_ends(
    isolated_home: Path,
) -> None:
    # The zero is sound over what was scanned, but the scan stops at #14 and
    # the caller has no way to know that — which is how "no one raised this"
    # gets stated about an issue filed after the archive was built.
    _seed(isolated_home)
    _seed_github(isolated_home)
    out = tool_grep_corpus("wg", "quantum annealing")
    assert "no matches" in out
    assert "org/repo through #14 (archive built 2026-08-06)" in out


def test_no_match_omits_the_record_edge_when_github_was_not_scanned(
    isolated_home: Path,
) -> None:
    # Scoped to mail: the GitHub ceiling bounds nothing the caller asked about,
    # so quoting it would be noise dressed as a caveat.
    _seed(isolated_home)
    _seed_github(isolated_home)
    out = tool_grep_corpus("wg", "quantum annealing", file_pattern="threads/*")
    assert "no matches" in out
    assert "org/repo through" not in out


def test_scoped_no_match_says_it_was_scoped(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail closed", file_pattern="drafts/*")
    assert "no matches" in out
    assert "`drafts/*`" in out
    # The warning that this is not absence from the corpus.
    assert "re-run without `file_pattern`" in out


def test_glob_matching_nothing_is_distinguished_from_no_match(
    isolated_home: Path,
) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail closed", file_pattern="minutes/*")
    # Nothing was scanned at all — a different finding from "scanned, absent".
    assert "no files match" in out
    assert "nothing was scanned" in out
    assert "**not** evidence of absence" in out


def test_zero_denominator_never_claims_evidence_of_absence(
    isolated_home: Path,
) -> None:
    """A scan that read no file cannot support a claim of absence, however it
    came to read none. Each exclusion route must say so rather than fall
    through to the evidence-of-absence body."""
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "meetings/ietf125/slides/deck.pdf", "fail closed\n"
    )
    write_cache_file(
        isolated_home, "wg", "raw/mail-archive-2026.txt", "fail closed\n"
    )
    # Only binaries matched; only derived duplicates matched; nothing matched.
    for glob in ("*.pdf", "*mail-archive*", "minutes/*"):
        out = tool_grep_corpus("wg", "fail closed", file_pattern=glob)
        assert "real evidence of absence" not in out, glob
        assert "nothing was scanned" in out, glob
        assert "**not** evidence of absence" in out, glob


def test_only_derived_matched_explains_how_to_reach_them(
    isolated_home: Path,
) -> None:
    _seed(isolated_home)
    write_cache_file(isolated_home, "wg", "raw/mail-archive-2026.txt", "fail closed\n")
    # A glob that reaches a derived file without naming the directory: the
    # default exclusion still applies, so the caller is told how to override it.
    out = tool_grep_corpus("wg", "fail closed", file_pattern="*mail-archive*")
    assert "duplicate file(s)" in out
    assert "starting `raw/`" in out


# --- discoverability the index does not have ---------------------------------


def test_finds_content_with_no_index_at_all(isolated_home: Path) -> None:
    """The #108 case: content on disk is findable without an embedding index,
    which is what makes a superseded draft revision (never embedded)
    discoverable at all."""
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "widget parameter")
    assert "1 matching line(s)" in out
    assert "drafts/draft-example-thing-00.txt" in out


def test_chunk_attribution_when_the_index_knows(isolated_home: Path) -> None:
    _seed(isolated_home)
    _build_with_stub("wg")
    out = tool_grep_corpus("wg", "fail closed here")
    # The hit is annotated with the chunk containing it, so it can be read
    # whole with get_chunk_text without a lookup round-trip.
    assert "[chunk " in out
    assert "Alice" in out


def test_no_chunk_tag_for_unindexed_file(isolated_home: Path) -> None:
    # `digests/` is never embedded (see embeddings._eligible_files), so a hit
    # there exercises the has-index-but-not-this-file path: attribution is
    # best-effort and must simply be absent, not wrong.
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "digests/citations.md", "cites the widget parameter\n"
    )
    _build_with_stub("wg")
    out = tool_grep_corpus("wg", "cites the widget", files_only=False)
    assert "digests/citations.md" in out
    assert "[chunk " not in out


# --- duplicate surfaces ------------------------------------------------------


def test_raw_and_github_are_skipped_by_default_and_reported(
    isolated_home: Path,
) -> None:
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "raw/mail-archive-2026.txt", "we should fail closed\n"
    )
    write_cache_file(
        isolated_home, "wg", "github/org-repo.json", '{"body": "fail closed"}\n'
    )
    out = tool_grep_corpus("wg", "fail closed")
    # Only the threads/ copy counts, and the skip is stated — not silent.
    assert "2 matching line(s) in 1 file(s)" in out
    assert "raw/mail-archive-2026.txt" not in out
    assert "2 duplicate file(s)" in out


def test_explicit_raw_glob_opts_back_in(isolated_home: Path) -> None:
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "raw/mail-archive-2026.txt", "we should fail closed\n"
    )
    out = tool_grep_corpus("wg", "fail closed", file_pattern="raw/*")
    assert "1 matching line(s)" in out
    assert "raw/mail-archive-2026.txt" in out


# --- bounded output ----------------------------------------------------------


def test_limit_caps_the_listing_but_not_the_count(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/many.md", "needle\n" * 30
    )
    out = tool_grep_corpus("wg", "needle", limit=5)
    # The total is the true total; the truncation is announced.
    assert "30 matching line(s)" in out
    assert "Showing 5 of 30" in out
    assert out.count("needle") >= 5


def test_limit_is_clamped_to_the_hard_cap(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/many.md", "needle\n" * (_MAX_GREP_LIMIT + 50)
    )
    out = tool_grep_corpus("wg", "needle", limit=10_000)
    assert f"Showing {_MAX_GREP_LIMIT} of {_MAX_GREP_LIMIT + 50}" in out


# --- max_per_file ------------------------------------------------------------


def _noisy_and_quiet(home: Path) -> None:
    """`a-noisy` sorts first and holds 40 hits; three quiet files follow."""
    write_cache_file(home, "wg", "threads/a-noisy.md", "needle noisy\n" * 40)
    for name in ("b", "c", "d"):
        write_cache_file(home, "wg", f"threads/{name}-quiet.md", "x\nneedle quiet\n")


def test_noisy_file_starves_the_rest_without_the_cap(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    out = tool_grep_corpus("wg", "needle", limit=10)
    assert "needle quiet" not in out
    assert "## threads/b-quiet.md" not in out


def test_max_per_file_lists_every_file(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    out = tool_grep_corpus("wg", "needle", limit=10, max_per_file=2)
    for name in ("a-noisy", "b-quiet", "c-quiet", "d-quiet"):
        assert f"## threads/{name}.md" in out
    assert out.count("needle noisy") == 2
    assert out.count("needle quiet") == 3


def test_max_per_file_keeps_full_counts_and_attributes_omissions(
    isolated_home: Path,
) -> None:
    _noisy_and_quiet(isolated_home)
    out = tool_grep_corpus("wg", "needle", limit=10, max_per_file=2)
    assert "43 matching line(s) in 4 file(s)" in out
    # Per-file true count survives the cap.
    assert "2 of 40 matching line(s) in this file shown" in out
    # Cap omitted 38; limit (10) is not reached by the 5 lines left.
    assert "Showing 5 of 43" in out
    assert "38 omitted by `max_per_file=2` (1 file(s) had more)" in out
    assert "omitted by `limit`" not in out


def test_max_per_file_and_limit_both_cut(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    out = tool_grep_corpus("wg", "needle", limit=3, max_per_file=2)
    # After the cap: 2 + 3 quiet = 5 lines; limit 3 keeps 2 noisy + 1 quiet.
    assert "Showing 3 of 43" in out
    assert "38 omitted by `max_per_file=2`" in out
    assert "2 omitted by `limit`" in out
    assert "2 matching file(s) have no line listed" in out
    assert "## threads/b-quiet.md" in out
    assert "## threads/c-quiet.md" not in out


def _advice(out: str) -> str:
    """The `Raise …` sentence of the footer, which names the caps to turn."""
    return next(line for line in out.splitlines() if "Raise " in line)


def test_footer_advises_only_the_cap_that_cut_lines(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    # Cap cut 38 lines, `limit` none: raise the cap, not `limit`.
    cap_only = _advice(tool_grep_corpus("wg", "needle", limit=10, max_per_file=2))
    assert "Raise `max_per_file`," in cap_only
    assert "`limit`" not in cap_only
    # Cap larger than every file cut nothing; `limit` did all the cutting.
    limit_only_out = tool_grep_corpus("wg", "needle", limit=4, max_per_file=100)
    assert "omitted by `max_per_file" not in limit_only_out
    limit_only = _advice(limit_only_out)
    assert "Raise `limit`" in limit_only
    assert "`max_per_file`" not in limit_only
    # Both cut: both are named.
    both = _advice(tool_grep_corpus("wg", "needle", limit=3, max_per_file=2))
    assert "`max_per_file` or `limit`" in both


def test_footer_does_not_blame_the_cap_when_limit_binds(isolated_home: Path) -> None:
    for name in "abcde":
        write_cache_file(isolated_home, "wg", f"threads/{name}.md", "needle\n")
    out = tool_grep_corpus("wg", "needle", limit=3, max_per_file=1)
    assert "2 omitted by `limit`" in out
    assert "`max_per_file=1`" not in out.split("capped:")[1]
    assert "`max_per_file`" not in _advice(out)


def test_no_footer_when_nothing_was_cut(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    out = tool_grep_corpus("wg", "needle", limit=200, max_per_file=100)
    assert "Showing" not in out
    assert "Raise" not in out


def test_max_per_file_larger_than_any_file_changes_nothing(
    isolated_home: Path,
) -> None:
    _noisy_and_quiet(isolated_home)
    plain = tool_grep_corpus("wg", "needle", limit=200)
    capped = tool_grep_corpus("wg", "needle", limit=200, max_per_file=500)
    assert capped == plain


def test_unset_max_per_file_is_unchanged(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/many.md", "needle\n" * 30)
    out = tool_grep_corpus("wg", "needle", limit=5)
    assert "Showing 5 of 30 matching line(s)" in out
    assert "max_per_file" not in out
    assert "in this file shown" not in out


def test_max_per_file_counts_matches_not_context(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home,
        "wg",
        "threads/ctx.md",
        "before\nneedle 1\nmid\nneedle 2\nafter\n",
    )
    out = tool_grep_corpus("wg", "needle", max_per_file=1, context=2)
    # One matching line is listed; its context lines are extra, not counted.
    assert "2: needle 1" in out
    assert "before" in out
    assert "4- needle 2" in out  # the unlisted match shows only as context
    assert "1 of 2 matching line(s) in this file shown" in out


def test_max_per_file_retains_bounded_hits(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/many.md", "needle\n" * 5000)
    captured: list[int] = []
    real_render = grep._render_hits

    def _spy(wg, hits, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(len(hits))
        return real_render(wg, hits, *args, **kwargs)

    grep._render_hits = _spy  # type: ignore[assignment]
    try:
        out = tool_grep_corpus("wg", "needle", limit=50, max_per_file=3)
    finally:
        grep._render_hits = real_render  # type: ignore[assignment]
    assert captured == [3]
    assert "5000 matching line(s)" in out


def test_max_per_file_ignored_under_files_only(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    capped = tool_grep_corpus("wg", "needle", files_only=True, max_per_file=1)
    assert capped == tool_grep_corpus("wg", "needle", files_only=True)


def test_invalid_max_per_file_is_refused(isolated_home: Path) -> None:
    _noisy_and_quiet(isolated_home)
    for bad in (0, -1, "abc", True, False, float("inf"), float("nan")):
        out = tool_grep_corpus("wg", "needle", max_per_file=bad)  # type: ignore[arg-type]
        assert "`max_per_file` to be a positive integer" in out
        assert "matching line(s)" not in out


def test_files_only_is_one_row_per_file(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail closed", files_only=True)
    assert "2 match(es)  threads/2026-04-10-mlkem.md" in out
    # The lines themselves are not rendered in this mode.
    assert "safe default" not in out


def test_context_lines_are_included(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "FAIL CLOSED", case_sensitive=True, context=2)
    assert "_Subject:_ Re: MLKEM" in out


def test_long_lines_are_elided(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "threads/long.md", "needle " + ("x" * 5000) + "\n"
    )
    out = tool_grep_corpus("wg", "needle")
    assert "…" in out
    assert len(max(out.splitlines(), key=len)) < 1000


def test_elision_keeps_the_match_visible(isolated_home: Path) -> None:
    """Truncating from the left would render a hit line with no match in it —
    exactly the case the cap exists for (a pasted log line, a base64 blob)."""
    write_cache_file(
        isolated_home, "wg", "threads/long.md", ("x" * 3000) + " needle here\n"
    )
    out = tool_grep_corpus("wg", "needle")
    assert "1 matching line(s)" in out
    hit_line = next(line for line in out.splitlines() if line.strip().startswith("1:"))
    assert "needle" in hit_line
    assert len(hit_line) < 1000


def test_retained_hits_are_bounded_by_limit(isolated_home: Path) -> None:
    """The count must stay complete without holding a hit per match — on a real
    corpus a one-character pattern matches over a million lines, which on the
    shared HTTP deployment would be a memory-exhaustion lever."""
    write_cache_file(isolated_home, "wg", "threads/many.md", "needle\n" * 5000)
    captured: list[int] = []
    real_render = grep._render_hits

    def _spy(wg, hits, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(len(hits))
        return real_render(wg, hits, *args, **kwargs)

    grep._render_hits = _spy  # type: ignore[assignment]
    try:
        out = tool_grep_corpus("wg", "needle", limit=10)
    finally:
        grep._render_hits = real_render  # type: ignore[assignment]
    assert captured == [10]
    assert "5000 matching line(s)" in out


def test_files_only_retains_no_hits(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/many.md", "needle\n" * 5000)
    out = tool_grep_corpus("wg", "needle", files_only=True)
    assert "5000 match(es)  threads/many.md" in out
    assert "5000 matching line(s) across 1 file(s)" in out


def test_files_only_keeps_the_pivot_footer(isolated_home: Path) -> None:
    _seed(isolated_home)
    out = tool_grep_corpus("wg", "fail closed", files_only=True)
    assert "read_file_section" in out


# --- safety ------------------------------------------------------------------


def test_unknown_corpus_is_refused(isolated_home: Path) -> None:
    out = tool_grep_corpus("nope", "anything")
    assert "Unknown corpus" in out


def test_binary_files_are_skipped_and_reported(isolated_home: Path) -> None:
    _seed(isolated_home)
    write_cache_file(
        isolated_home, "wg", "meetings/ietf125/slides/deck.pdf", "needle\n"
    )
    out = tool_grep_corpus("wg", "needle")
    assert "no matches" in out
    assert "1 binary file(s) skipped" in out


def test_chunk_attribution_is_right_for_blank_led_draft(isolated_home: Path) -> None:
    # Datatracker drafts start with blank lines. Chunk line spans once ignored
    # them, so a hit within a few lines of a window boundary was tagged with
    # the neighbouring chunk (and the file's last lines with none).
    import re

    rel = "drafts/draft-ietf-foo-bar-00.txt"
    rows = [
        f"row-{n:04d} some filler text for the draft row-{n:04d}" for n in range(1, 201)
    ]
    raw = "\n\n\n\n" + "\n".join(rows) + "\n"
    write_cache_file(isolated_home, "wg", rel, raw)
    _build_with_stub("wg")

    out = tool_grep_corpus("wg", r"^row-\d+", regex=True, limit=_MAX_GREP_LIMIT)
    seen = 0
    for line in out.splitlines():
        if not re.match(r"\s+\d+: row-", line):
            continue
        hit = re.match(r"\s+(\d+): (row-\d+).*\[chunk (\d+):", line)
        assert hit, f"untagged hit: {line!r}"
        lineno, needle, chunk_idx = int(hit[1]), hit[2], int(hit[3])
        assert rows[lineno - 5].startswith(needle)
        got = embeddings.get_chunk("wg", rel, chunk_idx)
        assert got is not None
        # The tagged chunk's characters overlap the matched line's.
        chunk_at = raw.index(got[1])
        line_at = raw.index(rows[lineno - 5])
        assert chunk_at < line_at + len(rows[lineno - 5])
        assert line_at < chunk_at + len(got[1])
        seen += 1
    assert seen == len(rows)
