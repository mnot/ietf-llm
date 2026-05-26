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
