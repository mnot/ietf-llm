"""Tests for the pure tool-function bodies in ietf_llm.mcp_server.

Only the no-network pieces:
- _safe_path: must reject path traversal and absolute paths
- tool_read_file_section: must enforce its line cap
- tool_list_corpora: only WGs with a files/ subdir
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import mcp_server

from conftest import write_cache_file


# --- _safe_path ------------------------------------------------------------


def test_safe_path_resolves_valid_file(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt")
    resolved = mcp_server._safe_path("wg", "ok.txt")
    assert resolved is not None
    assert resolved.endswith("/ok.txt")


def test_safe_path_rejects_traversal(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt")
    # Even though /etc/passwd exists on most systems, the function
    # should refuse to resolve outside the WG's files/ dir.
    assert mcp_server._safe_path("wg", "../../../etc/passwd") is None
    assert mcp_server._safe_path("wg", "../../other-wg/files/x.txt") is None


def test_safe_path_returns_none_for_missing(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "exists.txt")
    assert mcp_server._safe_path("wg", "missing.txt") is None


# --- tool_read_file_section -----------------------------------------------


def test_read_file_section_respects_max_lines(isolated_home: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(1, 101))
    write_cache_file(isolated_home, "wg", "long.txt", content)
    out = mcp_server.tool_read_file_section("wg", "long.txt", start_line=1, max_lines=10)
    lines = out.splitlines()
    # 10 lines of content plus a "truncated" marker
    assert any(l.startswith("line 10") for l in lines)
    assert not any(l.startswith("line 11") for l in lines)
    assert any("truncated" in l for l in lines)


def test_read_file_section_rejects_oversized_request(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt", "hi")
    out = mcp_server.tool_read_file_section("wg", "ok.txt", max_lines=99999)
    assert "exceeds hard cap" in out


def test_read_file_section_supports_start_line(isolated_home: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(1, 21))
    write_cache_file(isolated_home, "wg", "long.txt", content)
    out = mcp_server.tool_read_file_section(
        "wg", "long.txt", start_line=10, max_lines=3
    )
    assert "line 10" in out
    assert "line 11" in out
    assert "line 12" in out
    assert "line 9" not in out


def test_read_file_section_rejects_path_traversal(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "ok.txt", "hi")
    out = mcp_server.tool_read_file_section("wg", "../../../etc/passwd")
    assert "not found" in out.lower()


# --- tool_list_corpora ---------------------------------------------


def test_list_corpora_only_wgs_with_files_dir(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg1", "x.txt")
    write_cache_file(isolated_home, "wg2", "x.txt")
    # A bare directory without files/ underneath shouldn't count.
    (isolated_home / ".cache" / "ietf-llm" / "stray").mkdir(parents=True)
    out = mcp_server.tool_list_corpora()
    assert "wg1" in out
    assert "wg2" in out
    assert "stray" not in out


def test_list_corpora_empty_message(isolated_home: Path) -> None:
    out = mcp_server.tool_list_corpora()
    # Corpus-oriented empty message points the user at the gather CLI.
    assert "no corpora" in out.lower()
    assert "ietf-llm" in out.lower()


# --- read_digest ----------------------------------------------------------


def test_read_digest_people_kind_is_valid(isolated_home: Path) -> None:
    # "people" is one of the recognised kinds. With no file present
    # we get the not-found message; with the file present we get content.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "# people" in out


def test_read_digest_rejects_unknown_kind(isolated_home: Path) -> None:
    out = mcp_server.tool_read_digest("wg", "nonsense")
    # An *unknown* kind is distinguished from an ungathered one.
    assert "Unknown digest kind 'nonsense'" in out
    assert "people" in out  # still lists the valid kinds
    assert "Valid kinds" in out


def test_read_digest_absent_kind_reports_what_corpus_has(isolated_home: Path) -> None:
    # `issues` is a valid kind, but this corpus gathered no GitHub repos.
    # The message should list what IS present, not the universal set, and
    # must not read as if `issues` were an invalid kind.
    write_cache_file(isolated_home, "wg", "digests/threads.md", "# threads\n")
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "issues")
    assert "no 'issues' digest" in out
    assert "This corpus has: threads, people" in out
    assert "Unknown digest kind" not in out  # not treated as invalid
    assert "GitHub" in out  # explains why issues are absent


def test_read_digest_no_digests_at_all(isolated_home: Path) -> None:
    out = mcp_server.tool_read_digest("wg", "threads")
    assert "No digests for wg yet" in out


def test_collapse_draft_versions() -> None:
    from ietf_llm.mcp_server import _collapse_draft_versions

    class _H:
        def __init__(self, file: str) -> None:
            self.file = file

    hits = [
        _H("drafts/draft-ietf-httpbis-rfc6265bis-04.txt"),
        _H("drafts/draft-ietf-httpbis-rfc6265bis-22.txt"),  # newest → kept
        _H("drafts/draft-ietf-httpbis-rfc6265bis-12.txt"),
        _H("drafts/rfc9110.txt"),  # RFC, no rev → kept
        _H("threads/2026-01-01-x.md"),  # non-draft → kept
        _H("drafts/draft-ietf-httpbis-other-01.txt"),  # different stem → kept
    ]
    kept, dropped = _collapse_draft_versions(hits)
    files = {h.file for h in kept}
    assert "drafts/draft-ietf-httpbis-rfc6265bis-22.txt" in files
    assert "drafts/draft-ietf-httpbis-rfc6265bis-04.txt" not in files
    assert "drafts/draft-ietf-httpbis-rfc6265bis-12.txt" not in files
    assert "drafts/rfc9110.txt" in files
    assert "threads/2026-01-01-x.md" in files
    assert "drafts/draft-ietf-httpbis-other-01.txt" in files
    assert dropped == 2


def test_collapse_draft_versions_single_rev_kept() -> None:
    from ietf_llm.mcp_server import _collapse_draft_versions

    class _H:
        def __init__(self, file: str) -> None:
            self.file = file

    hits = [_H("drafts/draft-ietf-httpbis-only-03.txt")]
    kept, dropped = _collapse_draft_versions(hits)
    assert len(kept) == 1 and dropped == 0


def test_list_labels_prefix_example_is_templated(isolated_home: Path) -> None:
    # The subject-prefix example must use one of THIS corpus's prefixes,
    # not a hardcoded `[mlkem]` (which is a TLS prefix).
    write_cache_file(
        isolated_home,
        "wg",
        "threads/2026-01-01-x.md",
        "### [1] Re: [foo] hello\n\n_Subject:_ Re: [foo] hello\n\nbody\n",
    )
    out = mcp_server.tool_list_labels("wg")
    assert "[mlkem]" not in out
    if "subject=" in out:
        assert "[foo]" in out


def test_top_level_response_carries_freshness_line_when_fresh(
    isolated_home: Path,
) -> None:
    # _with_freshness now prepends the gather date to every top-level
    # response, not only when the cache is stale.
    from ietf_llm.freshness import record_gather

    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    record_gather("wg")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "gathered" in out.lower()
    assert "# people" in out


def test_top_level_response_silent_when_no_sentinel(isolated_home: Path) -> None:
    # Legacy cache with no sentinel: no freshness line, just the body.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "gathered" not in out.lower()


# --- get_chunk_text digest-file hint + chunk-not-found hints ---------------


def test_get_chunk_on_digest_file_redirects_to_read_digest(
    isolated_home: Path,
) -> None:
    # The consuming-LLM gap: get_chunk_text("aipref-_people.md", 0) used
    # to return an opaque "Chunk not found." Now it should name the
    # right tool to call.
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    out = mcp_server.tool_get_chunk("wg", "digests/people.md", 0)
    assert "digest" in out.lower()
    assert "read_digest" in out
    assert "kind='people'" in out


def test_get_chunk_unknown_file_explains_what_to_do(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")  # ensure cache exists
    out = mcp_server.tool_get_chunk("wg", "not-indexed.md", 0)
    # No DB / unindexed file → point at list_files or --embed, not silence.
    assert "list_files" in out or "--embed" in out


def test_read_file_section_on_digest_includes_hint(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "digests/issues.md", "# issues\n\nbody\n")
    out = mcp_server.tool_read_file_section("wg", "digests/issues.md")
    assert "read_digest" in out
    assert "kind='issues'" in out
    # And it still serves the content (just prefixed with the hint).
    assert "# issues" in out


# --- list_files chunk counts + digest annotation --------------------------


def test_list_files_annotates_digest_files(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/people.md", "x")
    write_cache_file(isolated_home, "wg", "digests/issues.md", "x")
    write_cache_file(isolated_home, "wg", "other.txt", "x")
    out = mcp_server.tool_list_files("wg")
    # Digest files should be flagged + redirect to read_digest.
    assert "digests/people.md" in out
    assert "read_digest" in out
    assert "kind='people'" in out
    assert "kind='issues'" in out


def test_list_files_distinguishes_not_indexed_from_no_chunks(
    isolated_home: Path,
) -> None:
    # Without an embedding index, files should show as "(not indexed)"
    # rather than "(no chunks)" — the latter implied the file was
    # genuinely empty when really the index just hadn't been built yet.
    write_cache_file(isolated_home, "wg", "drafts/draft-foo.txt", "body")
    out = mcp_server.tool_list_files("wg")
    assert "(not indexed)" in out
    assert "(no chunks)" not in out


def test_list_files_pattern_filter(isolated_home: Path) -> None:
    # The full inventory dump is unwieldy on large WGs; a glob filter
    # lets the consumer ask for just the slice they care about
    # without scrolling 600 lines of file names.
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-01-mlkem-debate.md", "x",
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-04-02-mlkem-followup.md", "x",
    )
    write_cache_file(
        isolated_home, "wg", "threads/2026-05-01-unrelated.md", "x",
    )
    out = mcp_server.tool_list_files("wg", pattern="threads/*mlkem*")
    assert "mlkem-debate" in out
    assert "mlkem-followup" in out
    assert "unrelated" not in out


def test_list_files_pattern_no_match_gives_helpful_message(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "x")
    out = mcp_server.tool_list_files("wg", pattern="threads/*mlkem*")
    assert "no files match" in out.lower()


# --- get_chunk_text range fetch ------------------------------------------


def test_get_chunk_range_rejects_inverted_bounds(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunk("wg", "x.txt", 5, end_chunk_idx=2)
    assert "less than" in out


def test_get_chunk_range_caps_size(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunk("wg", "x.txt", 0, end_chunk_idx=999)
    assert "max per call" in out


# --- freshness banner on top-level tools ----------------------------------


def _make_stale(wg: str, days: int) -> None:
    """Drop a backdated sentinel so staleness_warning fires."""
    from datetime import datetime, timedelta, timezone

    from ietf_llm.freshness import _sentinel_path

    when = datetime.now(timezone.utc) - timedelta(days=days)
    path = Path(_sentinel_path(wg))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(when.strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_overview_prepends_staleness_warning_when_stale(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg index\n")
    _make_stale("wg", days=30)
    out = mcp_server.tool_overview("wg")
    assert out.startswith("⚠")
    assert "30 days ago" in out
    # And the actual overview body still follows it.
    assert "overview" in out.lower()


def test_overview_omits_banner_when_fresh(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg index\n")
    from ietf_llm.freshness import record_gather

    record_gather("wg")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")


def test_read_digest_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/people.md", "# people\n")
    _make_stale("wg", days=14)
    out = mcp_server.tool_read_digest("wg", "people")
    assert out.startswith("⚠")
    assert "14 days ago" in out


def test_list_files_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "anything.txt", "hi")
    _make_stale("wg", days=10)
    out = mcp_server.tool_list_files("wg")
    assert out.startswith("⚠")


# --- read_digest include_bodies (consumer feedback) ---------------------


def test_read_digest_include_bodies_appends_issue_files(
    isolated_home: Path,
) -> None:
    # The single biggest workflow win from the latest consumer trace:
    # include_bodies=True lets a `label="X"` query return the catalogue
    # AND the issues' opening descriptions in ONE call, replacing N
    # follow-up read_file_section calls.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | "
            "Author | Participants | Dup-of | File |\n"
            "|---|-------|-------|--------|----------|---------|"
            "--------|--------------|--------|------|\n"
            "| 1 | OPEN | T1 | top-level | 0 | 2026-05-01 | "
            "Alice | | | `issues/org-repo/1.md` |\n"
            "| 2 | OPEN | T2 | top-level | 0 | 2026-05-02 | "
            "Bob | | | `issues/org-repo/2.md` |\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        (
            "# Issue #1: T1\n\n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-05-01 10:00 — Alice _(opened issue)_\n\n"
            "Opening argument body.\n\n"
            "## Comments\n\n"
            "### [2] 2026-05-02 10:00 — Bob\n\n"
            "Some long comment thread we DON'T want pulled in.\n"
        ),
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/2.md",
        (
            "# Issue #2: T2\n\n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-05-02 10:00 — Bob _(opened issue)_\n\n"
            "Different argument.\n"
        ),
    )
    out = mcp_server.tool_read_digest(
        "wg", "issues", label="top-level", include_bodies=True,
    )
    # Both bodies appear under an "Issue bodies" section.
    assert "## Issue bodies" in out
    assert "Opening argument body." in out
    assert "Different argument." in out
    # Comment threads are deliberately NOT pulled in (size discipline).
    assert "DON'T want pulled in" not in out


def test_read_digest_include_bodies_only_for_issues_kind(
    isolated_home: Path,
) -> None:
    # `include_bodies` is a no-op on non-issues kinds — it doesn't
    # error, it just doesn't add a Bodies section.
    write_cache_file(
        isolated_home, "wg", "digests/threads.md", "# threads\n\nbody\n",
    )
    out = mcp_server.tool_read_digest(
        "wg", "threads", include_bodies=True,
    )
    assert "Issue bodies" not in out


def test_read_digest_without_include_bodies_unchanged(
    isolated_home: Path,
) -> None:
    # Default behaviour (include_bodies=False) is unchanged.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        "# wg: issues\n\n## org/repo\n\n"
        "| # | State | Title | File |\n|---|-------|-------|------|\n"
        "| 1 | OPEN | T | `issues/org-repo/1.md` |\n",
    )
    out = mcp_server.tool_read_digest("wg", "issues")
    assert "Issue bodies" not in out


# --- Next-step pointers on discovery tools ------------------------------


def test_list_labels_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    # Consumer feedback: discovery tools (list_labels in particular)
    # are useless without their companion tools, but the harness
    # lazy-loads. Embedding concrete next-call signatures in the
    # output gives the consuming LLM the names it needs for the next
    # tool_search query.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | Author |\n"
            "|---|-------|-------|--------|----------|---------|--------|\n"
            "| 1 | OPEN | A | top-level | 1 | 2026-05-14 | Alice |\n"
        ),
    )
    out = mcp_server.tool_list_labels("wg")
    assert "read_digest" in out
    assert "include_bodies=True" in out  # the recommended new shape
    assert "search_corpus" in out


def test_list_files_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "anything.txt", "hi")
    out = mcp_server.tool_list_files("wg")
    assert "read_file_section" in out
    assert "get_chunk_text" in out


def test_list_corpora_includes_next_call_signatures(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_list_corpora()
    assert "overview" in out
    assert "read_digest" in out
    assert "search_corpus" in out


# --- list_labels ----------------------------------------------------------


def test_list_labels_returns_frequencies(isolated_home: Path) -> None:
    # Consumer feedback: there was no list_labels tool, so the consumer
    # had to guess label="top-level". Expose the same data the overview
    # samples, but unbounded and as a dedicated tool.
    write_cache_file(
        isolated_home, "wg", "digests/issues.md",
        (
            "# wg: issues\n\n## org/repo\n\n"
            "| # | State | Title | Labels | Comments | Updated | Author |\n"
            "|---|-------|-------|--------|----------|---------|--------|\n"
            "| 1 | OPEN | A | top-level | 1 | 2026-05-14 | Alice |\n"
            "| 2 | OPEN | B | top-level, vocab | 1 | 2026-05-13 | Bob |\n"
            "| 3 | OPEN | C | vocab | 1 | 2026-05-12 | Carol |\n"
        ),
    )
    out = mcp_server.tool_list_labels("wg")
    # Both labels are listed with their counts.
    assert "`top-level` | 2" in out
    assert "`vocab` | 2" in out
    # Header records the total distinct count.
    assert "(2 distinct)" in out


def test_list_labels_handles_no_vocabulary(isolated_home: Path) -> None:
    # No issues digest AND no threads dir → friendly empty response,
    # not a crash. (The "no vocabulary" wording replaced the older
    # "no labels recorded" when the tool grew its subject-prefix
    # section.)
    write_cache_file(isolated_home, "wg", "digests/index.md", "# index\n")
    out = mcp_server.tool_list_labels("wg")
    assert "No curation vocabulary" in out


# --- GitHub URL surfacing on search hits ---------------------------------


def test_no_banner_when_sentinel_absent(isolated_home: Path) -> None:
    # Cache exists but freshness sentinel doesn't (legacy / pre-feature).
    # Per design we stay silent, not nag.
    write_cache_file(isolated_home, "wg", "digests/index.md", "# wg\n")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")


# --- fetch_by_url (consumer feedback #9) ---------------------------------


def test_fetch_by_url_returns_chunk_when_url_matches(
    isolated_home: Path,
) -> None:
    # Seed the chunks DB the long way: write a per-issue file with a
    # **URL:** line, then build the embedding index against it. The
    # chunker stamps the file-level URL onto every chunk; fetch_by_url
    # rounds-trips through that column.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        (
            "# Issue #1: T\n\n"
            "**Repository:** org/repo  \n"
            "**URL:** https://github.com/org/repo/issues/1  \n"
            "**State:** OPEN  \n\n"
            "## Description\n\n"
            "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\n"
            "the actual chunk body here.\n"
        ),
    )
    # Build the index via the stub-model fixture used by search tests.
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_fetch_by_url(
        "wg", "https://github.com/org/repo/issues/1",
    )
    assert "the actual chunk body here" in out
    # Header carries enough metadata for the caller to pivot.
    assert "issues/org-repo/1.md" in out


def test_fetch_by_url_resolves_w3_mid_archived_at(isolated_home: Path) -> None:
    # The Archived-At permalink stamped on every thread message is a
    # `www.w3.org/mid/<message-id>` URL — the form fetch_by_url must
    # resolve (not `mailarchive.ietf.org/...`).
    mid = "https://www.w3.org/mid/test-msgid-123@example.com"
    write_cache_file(
        isolated_home, "wg", "threads/2026-01-01-t.md",
        (
            "# Thread\n\n## Messages\n\n"
            "### [1] 2026-01-01 10:00 — Alice\n\n"
            "_Subject:_ Hello\n"
            f"_Archived-At:_ {mid}\n\n"
            "the message body to resolve.\n"
        ),
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_fetch_by_url("wg", mid)
    assert "the message body to resolve" in out


def test_fetch_by_url_returns_helpful_miss_message(
    isolated_home: Path,
) -> None:
    # Unknown URL → message that explains the supported forms, not silent
    # None — and names w3.org/mid so a consumer is not sent to mailarchive.
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_fetch_by_url("wg", "https://mailarchive.ietf.org/arch/msg/x/y/")
    assert "No cached chunk" in out
    assert "w3.org/mid" in out
    assert "ietf-llm wg" in out  # the recovery hint


# --- get_chunks_batch (cross-file batch reads) ---------------------------


def test_get_chunks_batch_concatenates_multiple_files(
    isolated_home: Path,
) -> None:
    # Seed two per-issue files; verify a single batch call returns
    # chunks from both, separated by per-file headers.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        "# Issue #1: T1\n\n## Description\n\n"
        "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\n"
        "first issue body.\n",
    )
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/2.md",
        "# Issue #2: T2\n\n## Description\n\n"
        "### [1] 2026-01-02 10:00 — Bob _(opened issue)_\n\n"
        "second issue body.\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "issues/org-repo/1.md", "chunk_idx": 1},
        {"file": "issues/org-repo/2.md", "chunk_idx": 1},
    ])
    assert "first issue body" in out
    assert "second issue body" in out
    # Per-file headers so the consumer can tell hits apart.
    assert "issues/org-repo/1.md @ chunk 1" in out
    assert "issues/org-repo/2.md @ chunk 1" in out


def test_get_chunks_batch_caps_total_chunks(isolated_home: Path) -> None:
    # 21 chunks across requests exceeds the cap. The error message
    # names the cap so the consumer can split sensibly.
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "f.md", "chunk_idx": 0, "end_chunk_idx": 20},
    ])
    assert "max per call is 20" in out


def test_get_chunks_batch_rejects_inverted_range(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "x.txt", "hi")
    out = mcp_server.tool_get_chunks_batch("wg", [
        {"file": "f.md", "chunk_idx": 5, "end_chunk_idx": 2},
    ])
    assert "must be >= chunk_idx" in out


def test_get_chunks_batch_tolerates_single_dict_input(
    isolated_home: Path,
) -> None:
    # Defensive: if the MCP serialiser passes a lone dict instead of
    # a 1-element list, treat as length-1 batch rather than erroring.
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        "# Issue #1: T\n\n## Description\n\n"
        "### [1] 2026-01-01 10:00 — Alice _(opened issue)_\n\nbody\n",
    )
    from test_search_filters import _build_with_stub  # noqa: F401

    _build_with_stub("wg", isolated_home)
    out = mcp_server.tool_get_chunks_batch(
        "wg",
        {"file": "issues/org-repo/1.md", "chunk_idx": 1},  # type: ignore[arg-type]
    )
    assert "body" in out


# --- _load_server_instructions --------------------------------------------


def test_load_server_instructions_strips_frontmatter() -> None:
    # The bundled SKILL.md has a YAML frontmatter block (name + description).
    # That's skill metadata, not model guidance; the loader must strip it.
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert out is not None
    # The opening line of the body is "# ietf-llm" (the markdown heading).
    # The frontmatter's "---" sentinels must NOT survive.
    assert out.lstrip().startswith("#")
    assert "---" not in out.splitlines()[0]


def test_load_server_instructions_includes_load_bearing_content() -> None:
    # Spot-check that the routing rules and IETF norms the skill carries
    # actually land in the instructions string. If this regresses, the
    # non-Claude harnesses lose the guidance.
    out = mcp_server._load_server_instructions()  # pylint: disable=protected-access
    assert out is not None
    # Routing rules.
    assert "overview" in out
    assert "read_digest" in out
    assert "search_corpus" in out
    # IETF norms — the load-bearing interpretive guidance.
    assert "Consensus is chair-declared" in out
    assert "Decisions happen on the mailing list" in out
