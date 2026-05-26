"""Tests for the pure tool-function bodies in ietf_llm.mcp_server.

Only the no-network pieces:
- _safe_path: must reject path traversal and absolute paths
- tool_read_file_section: must enforce its line cap
- tool_list_working_groups: only WGs with a files/ subdir
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


# --- tool_list_working_groups ---------------------------------------------


def test_list_working_groups_only_wgs_with_files_dir(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg1", "x.txt")
    write_cache_file(isolated_home, "wg2", "x.txt")
    # A bare directory without files/ underneath shouldn't count.
    (isolated_home / ".cache" / "ietf-llm" / "stray").mkdir(parents=True)
    out = mcp_server.tool_list_working_groups()
    assert "wg1" in out
    assert "wg2" in out
    assert "stray" not in out


def test_list_working_groups_empty_message(isolated_home: Path) -> None:
    out = mcp_server.tool_list_working_groups()
    assert "no working groups" in out.lower()


# --- read_digest ----------------------------------------------------------


def test_read_digest_people_kind_is_valid(isolated_home: Path) -> None:
    # "people" is one of the recognised kinds. With no file present
    # we get the not-found message; with the file present we get content.
    write_cache_file(isolated_home, "wg", "wg-_people.md", "# people\n")
    out = mcp_server.tool_read_digest("wg", "people")
    assert "# people" in out


def test_read_digest_rejects_unknown_kind(isolated_home: Path) -> None:
    out = mcp_server.tool_read_digest("wg", "nonsense")
    # Error message names all valid kinds, including the new "people".
    assert "people" in out
    assert "Valid kinds" in out


# --- get_chunk_text digest-file hint + chunk-not-found hints ---------------


def test_get_chunk_on_digest_file_redirects_to_read_digest(
    isolated_home: Path,
) -> None:
    # The consuming-LLM gap: get_chunk_text("aipref-_people.md", 0) used
    # to return an opaque "Chunk not found." Now it should name the
    # right tool to call.
    write_cache_file(isolated_home, "wg", "wg-_people.md", "# people\n")
    out = mcp_server.tool_get_chunk("wg", "wg-_people.md", 0)
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
    write_cache_file(isolated_home, "wg", "wg-_issues.md", "# issues\n\nbody\n")
    out = mcp_server.tool_read_file_section("wg", "wg-_issues.md")
    assert "read_digest" in out
    assert "kind='issues'" in out
    # And it still serves the content (just prefixed with the hint).
    assert "# issues" in out


# --- list_files chunk counts + digest annotation --------------------------


def test_list_files_annotates_digest_files(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "wg-_people.md", "x")
    write_cache_file(isolated_home, "wg", "wg-_issues.md", "x")
    write_cache_file(isolated_home, "wg", "other.txt", "x")
    out = mcp_server.tool_list_files("wg")
    # Digest files should be flagged + redirect to read_digest.
    assert "wg-_people.md" in out
    assert "read_digest" in out
    assert "kind='people'" in out
    assert "kind='issues'" in out


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
    write_cache_file(isolated_home, "wg", "wg-_index.md", "# wg index\n")
    _make_stale("wg", days=30)
    out = mcp_server.tool_overview("wg")
    assert out.startswith("⚠")
    assert "30 days ago" in out
    # And the actual overview body still follows it.
    assert "overview" in out.lower()


def test_overview_omits_banner_when_fresh(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "wg-_index.md", "# wg index\n")
    from ietf_llm.freshness import record_gather

    record_gather("wg")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")


def test_read_digest_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "wg-_people.md", "# people\n")
    _make_stale("wg", days=14)
    out = mcp_server.tool_read_digest("wg", "people")
    assert out.startswith("⚠")
    assert "14 days ago" in out


def test_list_files_prepends_staleness_warning(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "anything.txt", "hi")
    _make_stale("wg", days=10)
    out = mcp_server.tool_list_files("wg")
    assert out.startswith("⚠")


def test_no_banner_when_sentinel_absent(isolated_home: Path) -> None:
    # Cache exists but freshness sentinel doesn't (legacy / pre-feature).
    # Per design we stay silent, not nag.
    write_cache_file(isolated_home, "wg", "wg-_index.md", "# wg\n")
    out = mcp_server.tool_overview("wg")
    assert not out.startswith("⚠")
