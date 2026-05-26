"""Tests for faceted search and chunk_date extraction.

Index-side tests use a stub embedding model so they're fast and
deterministic — no HuggingFace, no API calls. The stub just returns
a constant 8-dim vector for every text, so cosine similarity is 1.0
between any two embeddings; that means the model can't *rank* hits,
but it's still useful for exercising the filter SQL: every chunk
either matches the WHERE clause or doesn't, and the test asserts on
which ones come back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from ietf_llm import embeddings
from ietf_llm.embeddings.chunking import _normalize_to_utc_iso
from ietf_llm.embeddings.search import build_index, search
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


# --- _normalize_to_utc_iso -------------------------------------------------


def test_normalize_rfc5322_utc() -> None:
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00 +0000") == "2025-01-01T10:00:00Z"


def test_normalize_rfc5322_other_tz_converts_to_utc() -> None:
    # 2025-01-01 10:00:00 +0500 should become 05:00:00 UTC.
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00 +0500") == "2025-01-01T05:00:00Z"


def test_normalize_rfc5322_naive_assumed_utc() -> None:
    assert _normalize_to_utc_iso("Mon, 01 Jan 2025 10:00:00") == "2025-01-01T10:00:00Z"


def test_normalize_github_format() -> None:
    # github.py's format_date produces "YYYY-MM-DD HH:MM:SS UTC".
    assert _normalize_to_utc_iso("2025-01-01 10:00:00 UTC") == "2025-01-01T10:00:00Z"


def test_normalize_malformed_returns_none() -> None:
    assert _normalize_to_utc_iso("not a date") is None
    assert _normalize_to_utc_iso("") is None


# --- Index-with-stub-model fixtures ----------------------------------------


class _StubModel:
    """Returns a fixed unit vector for any text. Lets search() run
    end-to-end without a real embedding backend."""

    def embed(self, _text: str) -> Iterable[float]:  # llm.EmbeddingModel API
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


def _seed_stub_model(model_name: str = "stub") -> None:
    """Inject the stub into the process-level model cache so build_index
    and search both skip the real loader."""
    embeddings._MODEL_CACHE[model_name] = _StubModel()  # pylint: disable=protected-access


def _build_with_stub(wg: str, isolated_home: Path) -> None:
    cache = get_wg_file_cache_dir(wg)
    _seed_stub_model("stub")
    build_index(wg, cache, model_name="stub", verbose=Verbosity.QUIET)


# --- chunk_date in indexed rows --------------------------------------------


def test_thread_chunks_get_chunk_date(isolated_home: Path) -> None:
    # Thread-file message sections carry per-message chunk_date.
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic-a.md",
        (
            "# Topic A\n\n"
            "**Span:** 2025-01-01 → 2025-01-01\n"
            "**Messages:** 1\n\n"
            "## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\n"
            "body\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search("wg", "anything", k=10, verbose=Verbosity.QUIET)
    hits_in_range = search(
        "wg", "anything", k=10,
        since="2024-01-01T00:00:00Z", until="2026-01-01T00:00:00Z",
        verbose=Verbosity.QUIET,
    )
    # Header chunk + one message chunk; only the message chunk is dated.
    assert len(hits) >= 1
    assert len(hits_in_range) >= 1


def test_windowed_chunks_have_null_chunk_date_and_are_filtered_out(
    isolated_home: Path,
) -> None:
    write_cache_file(
        isolated_home, "wg", "draft-foo-00.txt", "Body of the draft. " * 100,
    )
    _build_with_stub("wg", isolated_home)
    # No date filter: chunks show up.
    assert search("wg", "x", k=5, verbose=Verbosity.QUIET)
    # With a date filter: chunks with NULL chunk_date are excluded.
    assert (
        search(
            "wg", "x", k=5, since="2024-01-01T00:00:00Z",
            verbose=Verbosity.QUIET,
        )
        == []
    )


# --- Filter behaviour -------------------------------------------------------


def _seed_two_files(isolated_home: Path) -> None:
    """Seed a thread file (dated 2025-01-01) and a per-issue file
    (dated 2026-06-02). The legacy `<wg>-github-<repo>.txt` blob is
    no longer indexed — per-issue .md files cover its content with
    proper per-message chunking."""
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-mail-topic.md",
        (
            "# Mail topic\n\n"
            "**Span:** 2025-01-01 → 2025-01-01\n\n"
            "## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\nfoo body\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1: Issue topic\n\n"
            "**Repository:** org/repo  \n"
            "**State:** OPEN  \n"
            "**Labels:** Vocabulary, Top-Level, ready to close  \n\n"
            "## Description\n\n"
            "### [1] 2026-06-02 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )


def test_file_pattern_filter_restricts_to_thread_files(
    isolated_home: Path,
) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-thread-%", verbose=Verbosity.QUIET,
    )
    assert all("-thread-" in h.file for h in hits)
    assert len(hits) >= 1


def test_file_pattern_filter_restricts_to_github(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    assert all("-issue-" in h.file for h in hits)
    assert hits


def test_since_filter_includes_only_newer_chunks(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # Thread message dated 2025-01-01; issue 2026-06-02. since=2026
    # should leave only the issue chunk.
    hits = search(
        "wg", "x", k=10, since="2026-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert all("-issue-" in h.file for h in hits)
    assert hits


def test_until_filter_includes_only_older_chunks(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, until="2026-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert all("-thread-" in h.file for h in hits)
    assert hits


def test_combined_filters_can_return_empty(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # 2030+ contains nothing.
    hits = search(
        "wg", "x", k=10, since="2030-01-01T00:00:00Z", verbose=Verbosity.QUIET,
    )
    assert hits == []


# --- label filter (per-issue file labels in the chunks index) -------------


def test_issue_chunks_carry_lowercased_labels(isolated_home: Path) -> None:
    # The seed file uses mixed case in the `**Labels:**` header line;
    # the chunker lowercases + comma-normalises for predictable LIKE
    # filtering at search time.
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    # Every chunk in the issue file inherits the same file-level labels.
    assert hits
    for hit in hits:
        assert hit.labels == "vocabulary,top-level,ready to close"


def test_thread_chunks_have_no_labels(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-thread-%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.labels is None for h in hits)


def test_label_filter_matches_substring_case_insensitively(
    isolated_home: Path,
) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    # Caller can pass any case; matched against the normalised column.
    hits = search(
        "wg", "x", k=10, label="Top-Level", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("top-level" in (h.labels or "") for h in hits)
    # Thread file (no labels at all) must NOT come back.
    assert all("-thread-" not in h.file for h in hits)


def test_label_filter_excludes_unlabelled_chunks(isolated_home: Path) -> None:
    # A label that isn't on the seeded issue should return nothing
    # (and definitely shouldn't surface thread/draft chunks, which
    # have labels=NULL).
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, label="nonexistent", verbose=Verbosity.QUIET,
    )
    assert hits == []


def test_issue_chunks_carry_normalised_state(isolated_home: Path) -> None:
    # The seed file uses uppercase "OPEN" in the **State:** line; the
    # chunker lowercases for predictable filtering.
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    assert hits
    for hit in hits:
        assert hit.state == "open"


def test_thread_chunks_have_no_state(isolated_home: Path) -> None:
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-thread-%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.state is None for h in hits)


def test_state_filter_excludes_other_states(isolated_home: Path) -> None:
    # Seed two issues: one OPEN, one CLOSED. state="closed" should
    # return only the closed one.
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1: Open question\n\n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nopen body\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-2.md",
        (
            "# Issue #2: Resolved question\n\n"
            "**State:** CLOSED  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-02 10:00 — Bob _(opened issue)_\n\nclosed body\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=20, state="closed", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.state == "closed" for h in hits)
    assert all("org-repo-2" in h.file for h in hits)


def test_state_filter_excludes_unstated_chunks(isolated_home: Path) -> None:
    # Threads and drafts have state=NULL — a state filter must not
    # return them.
    _seed_two_files(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=20, state="open", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("-thread-" not in h.file for h in hits)


def test_state_and_label_filters_compose(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-3.md",
        (
            "# Issue #3: Closed top-level\n\n"
            "**State:** CLOSED  \n"
            "**Labels:** top-level, ready to close  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-03 10:00 — Carol _(opened issue)_\n\nbody\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-4.md",
        (
            "# Issue #4: Closed unrelated\n\n"
            "**State:** CLOSED  \n"
            "**Labels:** something-else  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-04 10:00 — Dan _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=20,
        state="closed", label="top-level",
        verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("org-repo-3" in h.file for h in hits)


def test_issue_file_without_labels_line_gets_null_labels(
    isolated_home: Path,
) -> None:
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-2.md",
        (
            "# Issue #2: No labels here\n\n"
            "**Repository:** org/repo  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-06-02 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=10, file_pattern="%-issue-%", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all(h.labels is None for h in hits)


# --- mail-archive year-dump exclusion (consumer feedback #7) --------------


# --- sort="date" (chronological lens, consumer feedback #4) --------------


def _seed_chronological_issue(isolated_home: Path) -> None:
    """One issue file with four dated messages spanning Sept-Nov 2025,
    written out of order so a chronological sort has to do real work."""
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-155.md",
        (
            "# Issue #155: Category for RAG\n\n"
            "**State:** CLOSED  \n"
            "**Labels:** top-level  \n\n"
            "## Description\n\n"
            "### [1] 2025-09-15 10:00 — Alice _(opened issue)_\n\n"
            "Early objection: this overlaps with X.\n\n"
            "### [2] 2025-11-20 09:00 — Dan\n\n"
            "Settled position after chairs' resolution.\n\n"
            "### [3] 2025-10-14 14:30 — Martin Thomson\n\n"
            "IETF 124 clarification: actually the shape is Y.\n\n"
            "### [4] 2025-10-02 08:00 — Bob\n\n"
            "Mid-debate proposal.\n"
        ),
    )


def test_sort_date_returns_oldest_first(isolated_home: Path) -> None:
    # The consumer-feedback case: a debate evolving over weeks, where
    # relevance-ranking hides whether a position is early or settled.
    _seed_chronological_issue(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "anything", k=10, sort="date", verbose=Verbosity.QUIET,
    )
    # All four dated message chunks come back (the header chunk has
    # no chunk_date and is filtered out).
    titles = [h.title for h in hits]
    assert len(titles) == 4
    # Strictly ascending by the embedded YYYY-MM-DD prefix.
    assert titles[0].startswith("[1] 2025-09-15")
    assert titles[1].startswith("[4] 2025-10-02")
    assert titles[2].startswith("[3] 2025-10-14")
    assert titles[3].startswith("[2] 2025-11-20")


def test_sort_date_excludes_undated_chunks(isolated_home: Path) -> None:
    # Drafts / transcripts get NULL chunk_date; they have no place in
    # a chronological view of a debate and must be excluded.
    _seed_chronological_issue(isolated_home)
    write_cache_file(
        isolated_home, "wg", "draft-foo-00.txt",
        "draft body " * 200,  # windowed chunker, no chunk_date
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "anything", k=20, sort="date", verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("draft-foo" not in h.file for h in hits)


def test_sort_date_default_is_relevance(isolated_home: Path) -> None:
    # Without sort= the existing relevance behaviour is unchanged.
    # The stub model gives every chunk the same vector, so under
    # relevance ranking the result order is implementation-defined,
    # but it must NOT be ascending-by-date (since that's the new mode).
    _seed_chronological_issue(isolated_home)
    _build_with_stub("wg", isolated_home)
    hits_relevance = search(
        "wg", "anything", k=10, verbose=Verbosity.QUIET,
    )
    hits_date = search(
        "wg", "anything", k=10, sort="date", verbose=Verbosity.QUIET,
    )
    # Date-sorted result is strictly ordered; relevance result happens
    # to not be (or is by accident). The two orderings must differ for
    # the test corpus, otherwise sort="date" isn't doing any work.
    assert [h.title for h in hits_relevance] != [h.title for h in hits_date]


def test_sort_date_composes_with_file_pattern(isolated_home: Path) -> None:
    # Common use case from the docstring: scope chronological mode to
    # a single issue with file_pattern.
    _seed_chronological_issue(isolated_home)
    # Another issue we DON'T want in the result.
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-99.md",
        (
            "# Issue #99: Unrelated\n\n"
            "## Description\n\n"
            "### [1] 2025-10-10 10:00 — Eve _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    hits = search(
        "wg", "x", k=20, sort="date",
        file_pattern="%-issue-org-repo-155.md",
        verbose=Verbosity.QUIET,
    )
    assert hits
    assert all("org-repo-155" in h.file for h in hits)


def test_tool_search_summarises_uniform_closed_state(
    isolated_home: Path,
) -> None:
    # Consumer feedback: when every hit is from a closed issue, that's
    # the answer the user cares about. Surface it once as a result-set
    # summary instead of N times as a per-hit `[closed]` tag.
    from ietf_llm import mcp_server

    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1\n\n"
            "**State:** CLOSED  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-2.md",
        (
            "# Issue #2\n\n"
            "**State:** CLOSED  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-02 10:00 — Bob _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_search("wg", "x", k=10)
    # Summary line is present and concrete about what "all closed" implies.
    assert "All " in out and "closed" in out
    assert "resolved" in out.lower()
    # And it leads the output — not buried after the hit list.
    assert out.index("All ") < out.index("score=")


def test_tool_search_no_summary_when_states_mixed(isolated_home: Path) -> None:
    from ietf_llm import mcp_server

    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1\n\n**State:** OPEN  \n\n## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-2.md",
        (
            "# Issue #2\n\n**State:** CLOSED  \n\n## Description\n\n"
            "### [1] 2026-01-02 10:00 — Bob _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_search("wg", "x", k=10)
    # Mixed state → don't make claims about resolution.
    assert "All " not in out or "hits are from" not in out


def test_tool_search_surfaces_github_url_for_issue_hits(
    isolated_home: Path,
) -> None:
    # Consumer feedback (round N-1): file paths aren't citeable. For
    # issue chunks, the URL stamped on the chunk at index time should
    # surface as a `url:` row in each search hit.
    from ietf_llm import mcp_server

    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-1.md",
        (
            "# Issue #1: T\n\n"
            "**Repository:** org/repo  \n"
            "**URL:** https://github.com/org/repo/issues/1  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_search("wg", "x", k=5)
    assert "url: https://github.com/org/repo/issues/1" in out


def test_tool_search_surfaces_archived_at_for_thread_hits(
    isolated_home: Path,
) -> None:
    # Consumer feedback (this round): IETF mail archive Archived-At
    # permalinks should likewise surface in thread chunks. Each message
    # has its OWN URL, so different chunks in the same file get
    # different urls.
    from ietf_llm import mcp_server

    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic.md",
        (
            "# Topic\n\n"
            "## Messages\n\n"
            "### [1] 2025-01-01 10:00 — Alice\n\n"
            "_Subject:_ Topic\n"
            "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/aaa/\n\n"
            "body one\n\n"
            "### [2] 2025-01-02 10:00 — Bob\n\n"
            "_Subject:_ Re: Topic\n"
            "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/wg/bbb/\n\n"
            "body two\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_search("wg", "x", k=10)
    # Both message URLs appear, on their respective hits — and they're
    # different from each other (per-message, not per-file).
    assert "url: https://mailarchive.ietf.org/arch/msg/wg/aaa/" in out
    assert "url: https://mailarchive.ietf.org/arch/msg/wg/bbb/" in out


def test_tool_search_omits_url_for_unurled_chunks(isolated_home: Path) -> None:
    # Drafts / threads without Archived-At have no url — the `url:`
    # line must NOT appear.
    from ietf_llm import mcp_server

    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic.md",
        "# T\n\n### [1] 2025-01-01 10:00 — Alice\n\nbody (no archived-at)\n",
    )
    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_search("wg", "x", k=5)
    assert "url:" not in out


def test_search_hits_surface_duplicate_of(isolated_home: Path) -> None:
    # Consumer feedback: dup-of was in per-issue files and the digest,
    # but not in search hits. An LLM scanning hits should see "this
    # is a dup of #155" inline so they can skip the duplicate issues.
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-169.md",
        (
            "# Issue #169: Duplicate one\n\n"
            "**Repository:** org/repo  \n"
            "**State:** CLOSED  \n"
            "**Duplicate of:** #155\n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    from ietf_llm import mcp_server
    out = mcp_server.tool_search("wg", "x", k=5)
    assert "duplicate of: #155" in out


def test_search_hits_surface_closing_rationale(isolated_home: Path) -> None:
    # Closed-issue chunks should preview the closing rationale on a
    # `closing:` line, saving the consumer a file read just to learn
    # WHY the issue closed. The preview strips blockquote chrome and
    # the metadata byline so the substance fits on one line.
    write_cache_file(
        isolated_home, "wg", "wg-issue-org-repo-155.md",
        (
            "# Issue #155: Vocab decision\n\n"
            "**Repository:** org/repo  \n"
            "**State:** CLOSED  \n\n"
            "**Closing rationale:**\n\n"
            "_by Mark Nottingham (Chair) on 2026-02-01 10:00:_\n\n"
            "> Removed from the next set of drafts. This is\n"
            "> not a declaration of consensus to omit them.\n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n"
        ),
    )
    _build_with_stub("wg", isolated_home)
    from ietf_llm import mcp_server
    out = mcp_server.tool_search("wg", "x", k=5)
    # The rationale substance appears on a `closing:` line. Check the
    # specific line (not the whole output, since chunk-0's snippet
    # contains the full header text including the formatted rationale
    # block; we're only validating the inline `closing:` formatting).
    closing_lines = [
        line for line in out.splitlines() if "closing:" in line
    ]
    assert closing_lines
    rendered = closing_lines[0]
    assert "Removed from the next set of drafts" in rendered
    # Blockquote markers and byline must be stripped from the inline
    # form (they're chrome at preview size).
    assert "_by Mark Nottingham" not in rendered
    assert "> Removed" not in rendered


def test_search_hits_for_thread_chunks_omit_issue_signals(
    isolated_home: Path,
) -> None:
    # Thread chunks don't have dup-of / closing-rationale columns
    # populated — the per-hit renderer must NOT emit those lines.
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic.md",
        "# T\n\n### [1] 2025-01-01 10:00 — Alice\n\nbody\n",
    )
    _build_with_stub("wg", isolated_home)
    from ietf_llm import mcp_server
    out = mcp_server.tool_search("wg", "x", k=5)
    assert "duplicate of:" not in out
    assert "closing:" not in out


def test_mail_archive_year_dump_is_not_indexed(isolated_home: Path) -> None:
    # The legacy `<wg>-mail-archive-YYYY.txt` blob duplicates content
    # already covered by per-thread .md files. It must be excluded
    # from indexing so search hits are de-duplicated.
    write_cache_file(
        isolated_home, "wg", "wg-mail-archive-2025.txt",
        "Subject: legacy\nFrom: a\nDate: 2025-01-01\n\nbody\n",
    )
    write_cache_file(
        isolated_home, "wg", "wg-thread-2025-01-01-topic.md",
        "# T\n\n### [1] 2025-01-01 10:00 — Alice\n\nbody\n",
    )
    _build_with_stub("wg", isolated_home)
    hits = search("wg", "anything", k=20, verbose=Verbosity.QUIET)
    assert hits
    assert all("mail-archive" not in h.file for h in hits)
