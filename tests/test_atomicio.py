"""Tests for the directory-level primitives in `ietf_llm.atomicio`
(`swap_dir` / `scratch_sibling_name`), added for issue #224's follow-up:
`CloudCorpusStore.seed_workspace` and `seed.fetch._install_tree` both build
on these instead of hand-rolling the same move-aside/rename/restore dance.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ietf_llm.atomicio import scratch_sibling_name, swap_dir


def test_scratch_sibling_name_is_dot_prefixed(tmp_path: Path) -> None:
    # A leaked one must be invisible to paths.cached_wg_names(), which skips
    # dot- and underscore-prefixed entries.
    name = scratch_sibling_name(str(tmp_path / "tls"), "seed")
    assert os.path.dirname(name) == str(tmp_path)
    assert os.path.basename(name).startswith(".tls.seed.")


def test_scratch_sibling_name_is_unique_per_call(tmp_path: Path) -> None:
    a = scratch_sibling_name(str(tmp_path / "tls"), "seed")
    b = scratch_sibling_name(str(tmp_path / "tls"), "seed")
    assert a != b


def test_swap_dir_replaces_existing_dest(tmp_path: Path) -> None:
    dest = tmp_path / "corpus"
    dest.mkdir()
    (dest / "old.txt").write_text("old")
    new_tree = tmp_path / "new"
    new_tree.mkdir()
    (new_tree / "new.txt").write_text("new")

    old = swap_dir(str(dest), str(new_tree))

    assert (dest / "new.txt").read_text() == "new"
    assert not (dest / "old.txt").exists()
    assert old is not None
    assert Path(old).is_dir()
    assert (Path(old) / "old.txt").read_text() == "old"
    assert os.path.basename(old).startswith(".corpus.old.")


def test_swap_dir_no_prior_dest_returns_none(tmp_path: Path) -> None:
    dest = tmp_path / "corpus"
    new_tree = tmp_path / "new"
    new_tree.mkdir()
    (new_tree / "new.txt").write_text("new")

    old = swap_dir(str(dest), str(new_tree))

    assert old is None
    assert (dest / "new.txt").read_text() == "new"


def test_swap_dir_restores_dest_on_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "corpus"
    dest.mkdir()
    (dest / "old.txt").write_text("old")
    new_tree = tmp_path / "new"
    new_tree.mkdir()
    (new_tree / "new.txt").write_text("new")

    real_rename = os.rename

    def flaky(src: str, dst: str) -> None:
        if src == str(new_tree):
            raise OSError("boom")
        real_rename(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.rename", flaky)
    with pytest.raises(OSError):
        swap_dir(str(dest), str(new_tree))

    # dest is exactly as it was before the call -- nothing lost, and no
    # leftover ".old.<hex>" sibling from the failed attempt.
    assert (dest / "old.txt").read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus", "new"]


def test_swap_dir_no_prior_dest_on_rename_failure_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "corpus"  # does not exist yet
    new_tree = tmp_path / "new"
    new_tree.mkdir()
    (new_tree / "new.txt").write_text("new")

    def flaky(src: str, dst: str) -> None:
        raise OSError("boom")

    monkeypatch.setattr("ietf_llm.atomicio.os.rename", flaky)
    with pytest.raises(OSError):
        swap_dir(str(dest), str(new_tree))

    # Source untouched (rename never moves on failure), dest still absent.
    assert not dest.exists()
    assert (new_tree / "new.txt").read_text() == "new"
