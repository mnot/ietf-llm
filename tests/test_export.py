"""Tests for ietf_llm.export.directory — the local-mirror sink.

Verifies the four behaviours the user-facing contract promises:
- fresh export copies all eligible files
- re-export with no changes is a no-op
- stale files at the destination are pruned
- upstream file changes propagate
And that .json archives stay out of the mirror.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm import export
from ietf_llm.utils import Verbosity

from conftest import write_cache_file


def _mk_corpus(home: Path) -> None:
    write_cache_file(home, "wg", "wg-charter.txt", "charter")
    write_cache_file(home, "wg", "draft-foo-00.txt", "draft")
    write_cache_file(home, "wg", "wg-_index.md", "# digest")
    write_cache_file(home, "wg", "wg-github-x.json", "{}")  # excluded


def test_fresh_export_copies_text_files(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    names = sorted(p.name for p in dest.iterdir())
    assert names == ["draft-foo-00.txt", "wg-_index.md", "wg-charter.txt"]


def test_export_excludes_json(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert not (dest / "wg-github-x.json").exists()


def test_re_export_is_noop(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 0


def test_upstream_change_propagates(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    write_cache_file(isolated_home, "wg", "wg-charter.txt", "charter v2")
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 1
    assert (dest / "wg-charter.txt").read_text() == "charter v2"


def test_stale_dest_file_is_pruned(isolated_home: Path, tmp_path: Path) -> None:
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    # Drop a file at the dest that isn't in the cache.
    (dest / "stale-leftover.txt").write_text("stale")
    changes = export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert changes == 1
    assert not (dest / "stale-leftover.txt").exists()


def test_hidden_files_at_dest_preserved(isolated_home: Path, tmp_path: Path) -> None:
    # Pruning should leave dotfiles alone (e.g. .DS_Store, .gitignore).
    _mk_corpus(isolated_home)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".DS_Store").write_text("macos cruft")
    export.directory("wg", str(dest), verbose=Verbosity.QUIET)
    assert (dest / ".DS_Store").exists()


def test_no_cache_returns_zero(isolated_home: Path, tmp_path: Path) -> None:
    # Calling export without any prior gather should not crash, just
    # report zero work done.
    dest = tmp_path / "dest"
    changes = export.directory("wg-never-gathered", str(dest), verbose=Verbosity.QUIET)
    assert changes == 0
