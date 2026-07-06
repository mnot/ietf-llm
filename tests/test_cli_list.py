"""Tests for `ietf-llm --list` (cached-WG listing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.cli.list import discover_gathered_wgs, print_cached_wgs

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
    from ietf_llm.cli import list as cli_list
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


# --- all_corpora + --used-within recency filter ---------------------------


def test_all_corpora_uses_the_store(isolated_home: Path) -> None:
    from ietf_llm.cli.list import all_corpora

    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    assert all_corpora() == ["httpbis", "tls"]


def test_filter_recently_used_by_access_time(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    from ietf_llm.cli import list as cli_list

    now = datetime.datetime.now(datetime.timezone.utc)
    accessed = {
        "fresh": now - datetime.timedelta(days=2),
        "stale": now - datetime.timedelta(days=40),
    }

    class _Store:
        def last_accessed(self, name: str):
            return accessed.get(name)

        def gathered_at(self, name: str):
            return None

    monkeypatch.setattr(cli_list, "get_corpus_store", lambda: _Store())
    assert cli_list.filter_recently_used(["fresh", "stale"], 30) == ["fresh"]


def test_filter_falls_back_to_gather_time_when_never_accessed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime

    from ietf_llm.cli import list as cli_list

    now = datetime.datetime.now(datetime.timezone.utc)

    class _Store:
        def last_accessed(self, name: str):
            return None  # never read

        def gathered_at(self, name: str):
            # Gathered today -> within window despite no access record.
            return now if name == "newish" else now - datetime.timedelta(days=99)

    monkeypatch.setattr(cli_list, "get_corpus_store", lambda: _Store())
    assert cli_list.filter_recently_used(["newish", "ancient"], 30) == ["newish"]


def test_filter_keeps_corpus_with_no_timestamps(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm.cli import list as cli_list

    class _Store:
        def last_accessed(self, name: str):
            return None

        def gathered_at(self, name: str):
            return None

    monkeypatch.setattr(cli_list, "get_corpus_store", lambda: _Store())
    # Absence of information is not evidence of disuse -> keep it.
    assert cli_list.filter_recently_used(["mystery"], 30) == ["mystery"]
