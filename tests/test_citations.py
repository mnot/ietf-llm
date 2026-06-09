"""Tests for the draft → citing-files cross-reference.

The scanner walks per-thread and per-issue files for `draft-...`
references, normalises away the version suffix, dedupes per chunk,
and renders the result as `digests/citations.md`. The
`find_citations` MCP tool reads that digest by draft name.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import mcp_server
from ietf_llm.gather.citations import (
    citation_counts,
    normalize_draft_name,
    scan_citations,
    write_citations_digest,
)
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_cache_file


# --- normalize_draft_name -------------------------------------------------


def test_normalize_strips_version_suffix() -> None:
    assert normalize_draft_name("draft-foo-bar-07") == "draft-foo-bar"


def test_normalize_lowercases() -> None:
    assert normalize_draft_name("Draft-Foo-Bar") == "draft-foo-bar"


def test_normalize_leaves_unversioned_intact() -> None:
    assert normalize_draft_name("draft-foo-bar") == "draft-foo-bar"


# --- scan_citations -------------------------------------------------------


def _seed_thread(isolated_home: Path, slug: str, body: str) -> None:
    write_cache_file(
        isolated_home, "wg", f"threads/{slug}.md",
        "# Test\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\n"
        f"_Subject:_ T\n\n{body}\n"
    )


def test_scan_picks_up_draft_reference_in_thread(isolated_home: Path) -> None:
    _seed_thread(
        isolated_home, "topic-a",
        "I think draft-ietf-foo-bar addresses this point.",
    )
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    assert "draft-ietf-foo-bar" in out
    assert len(out["draft-ietf-foo-bar"]) == 1
    citation = out["draft-ietf-foo-bar"][0]
    assert citation.file == "threads/topic-a.md"
    assert citation.chunk_idx == 1
    assert "draft-ietf-foo-bar" in citation.context


def test_scan_strips_version_suffix(isolated_home: Path) -> None:
    # Citation `draft-ietf-foo-bar-07` should map to `draft-ietf-foo-bar`.
    _seed_thread(
        isolated_home, "topic-a",
        "See draft-ietf-foo-bar-07 section 4.",
    )
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    assert "draft-ietf-foo-bar" in out
    assert "draft-ietf-foo-bar-07" not in out


def test_scan_skips_quoted_blocks(isolated_home: Path) -> None:
    # A reply that quotes someone else's "see draft-foo" shouldn't
    # double-count — the original message already counts.
    write_cache_file(
        isolated_home, "wg", "threads/topic-a.md",
        "# Test\n\n## Messages\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\nSee draft-ietf-foo-bar.\n\n"
        "### [2] 2026-04-11 09:00 — Bob\n\n"
        "> See draft-ietf-foo-bar.\n\nAgreed.\n"
    )
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    # Only Alice's mention counts — Bob's was quoted.
    assert len(out["draft-ietf-foo-bar"]) == 1
    assert out["draft-ietf-foo-bar"][0].chunk_idx == 1


def test_scan_dedupes_repeated_mentions_in_one_chunk(
    isolated_home: Path,
) -> None:
    _seed_thread(
        isolated_home, "topic-a",
        "I'd reference draft-ietf-foo-bar — and again draft-ietf-foo-bar.",
    )
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    # One citation, not two — same draft in same chunk.
    assert len(out["draft-ietf-foo-bar"]) == 1


def test_scan_aggregates_across_threads(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "topic-a", "See draft-ietf-foo-bar.")
    _seed_thread(isolated_home, "topic-b", "Also draft-ietf-foo-bar applies.")
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    citing_files = {c.file for c in out["draft-ietf-foo-bar"]}
    assert citing_files == {
        "threads/topic-a.md", "threads/topic-b.md",
    }


def test_scan_handles_issues_too(isolated_home: Path) -> None:
    write_cache_file(
        isolated_home, "wg", "issues/org-repo/1.md",
        "# Issue\n\n## Comments\n\n"
        "### [1] 2026-04-10 09:00 — Alice\n\n"
        "Relates to draft-ietf-foo-bar.\n"
    )
    cache = get_wg_file_cache_dir("wg")
    out = scan_citations(cache, verbose=Verbosity.QUIET)
    assert out["draft-ietf-foo-bar"][0].file == "issues/org-repo/1.md"


# --- citation_counts ------------------------------------------------------


def test_citation_counts_collapses_to_int_map(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "a", "See draft-ietf-foo.")
    _seed_thread(isolated_home, "b", "Also draft-ietf-foo applies.")
    cache = get_wg_file_cache_dir("wg")
    counts = citation_counts(scan_citations(cache, verbose=Verbosity.QUIET))
    assert counts["draft-ietf-foo"] == 2


# --- write_citations_digest ----------------------------------------------


def test_write_citations_digest_renders_per_draft_sections(
    isolated_home: Path,
) -> None:
    _seed_thread(isolated_home, "a", "See draft-ietf-foo.")
    cache = get_wg_file_cache_dir("wg")
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    path = write_citations_digest(cache, citations, verbose=Verbosity.QUIET)
    assert path is not None
    text = Path(path).read_text()
    # Heading + draft name + the file it was cited in.
    assert "Draft citations" in text
    assert "`draft-ietf-foo`" in text
    assert "threads/a.md" in text


def test_write_citations_digest_removes_stale_on_empty(
    isolated_home: Path,
) -> None:
    # Gather overwrites digests file-by-file (no digests/ wipe), so a
    # re-gather that drops to zero citations must delete the old digest
    # rather than leave it serving stale edges. Build one, then re-run
    # the writer with empty input and assert the file is gone and the
    # tool reports the no-digest message.
    _seed_thread(isolated_home, "a", "See draft-ietf-foo-bar.")
    cache = get_wg_file_cache_dir("wg")
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    path = write_citations_digest(cache, citations, verbose=Verbosity.QUIET)
    assert path is not None and Path(path).is_file()
    # Re-gather with nothing cited (narrower window / quotes only).
    assert write_citations_digest(cache, {}, verbose=Verbosity.QUIET) is None
    assert not Path(path).exists()
    out = mcp_server.tool_find_citations("wg", "draft-ietf-foo-bar")
    assert "No citations digest" in out


# --- find_citations MCP tool ---------------------------------------------


def test_find_citations_returns_citing_files(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "a", "See draft-ietf-foo-bar.")
    _seed_thread(isolated_home, "b", "Also draft-ietf-foo-bar.")
    cache = get_wg_file_cache_dir("wg")
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    write_citations_digest(cache, citations, verbose=Verbosity.QUIET)
    out = mcp_server.tool_find_citations("wg", "draft-ietf-foo-bar")
    assert "threads/a.md" in out
    assert "threads/b.md" in out


def test_find_citations_tolerates_versioned_input(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "a", "See draft-ietf-foo-bar.")
    cache = get_wg_file_cache_dir("wg")
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    write_citations_digest(cache, citations, verbose=Verbosity.QUIET)
    # Caller passes the versioned form — the tool normalises and
    # finds the same entry.
    out = mcp_server.tool_find_citations("wg", "draft-ietf-foo-bar-07.txt")
    assert "threads/a.md" in out


def test_find_citations_friendly_when_unknown(isolated_home: Path) -> None:
    _seed_thread(isolated_home, "a", "See draft-ietf-foo-bar.")
    cache = get_wg_file_cache_dir("wg")
    citations = scan_citations(cache, verbose=Verbosity.QUIET)
    write_citations_digest(cache, citations, verbose=Verbosity.QUIET)
    out = mcp_server.tool_find_citations("wg", "draft-nobody-knows")
    assert "No citations" in out


def test_find_citations_no_digest_yet(isolated_home: Path) -> None:
    # WG cache exists but no citations.md (gather pre-citations or
    # nothing was cited). Friendly error pointing at re-gather.
    write_cache_file(isolated_home, "wg", "digests/index.md", "# x\n")
    out = mcp_server.tool_find_citations("wg", "draft-foo")
    assert "No citations digest" in out
