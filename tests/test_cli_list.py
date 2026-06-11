"""Tests for `ietf-llm --list` (cached-WG listing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.cli_list import discover_gathered_wgs, print_cached_wgs

from conftest import write_cache_file


def test_discover_gathered_wgs_finds_cached(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "x-foo", "digests/index.md", "# x\n")
    assert discover_gathered_wgs() == ["httpbis", "x-foo"]


def test_discover_skips_dotted_and_underscore_dirs(isolated_home: Path) -> None:
    # `_github-users.json` and similar machinery live at the cache root;
    # only dirs with a files/ subdir count.
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "_scratch", "digests/index.md", "# x\n")
    assert discover_gathered_wgs() == ["tls"]


def test_print_cached_wgs_lists_with_kind(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # A group corpus (charter.txt present) vs a synthetic one.
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "httpbis", "charter.txt", "charter\n")
    write_cache_file(isolated_home, "x-foo", "digests/index.md", "# x\n")
    rc = print_cached_wgs()
    assert rc == 0
    out = capsys.readouterr().out
    synthetic_line = next(ln for ln in out.splitlines() if ln.startswith("x-foo"))
    httpbis_line = next(ln for ln in out.splitlines() if ln.startswith("httpbis"))
    # The kind column distinguishes them.
    assert "synthetic" in synthetic_line
    assert "group" in httpbis_line
    assert "synthetic" not in httpbis_line
    # The status column disclaims a chartered effort rather than showing a
    # bare "—" that could read as an active group.
    assert "not an IETF effort" in synthetic_line
    assert "—" not in synthetic_line


def test_print_cached_wgs_shows_last_gathered(
    isolated_home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: object,
) -> None:
    import datetime
    from ietf_llm import cli_list
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    fake = datetime.datetime(2026, 5, 27, tzinfo=datetime.timezone.utc)
    # `cli_list` binds `last_gathered` at import time (from .freshness
    # import last_gathered), so patch the name where it's looked up.
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli_list, "last_gathered", lambda _wg: fake,
    )
    print_cached_wgs()
    out = capsys.readouterr().out
    assert "2026-05-27" in out


def test_print_cached_wgs_empty(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = print_cached_wgs()
    assert rc == 1
    err = capsys.readouterr().err
    assert "No corpora cached" in err
