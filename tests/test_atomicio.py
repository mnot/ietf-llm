"""Tests for the directory-level primitives in `ietf_llm.atomicio`
(`swap_dir` / `swap_dirs` / `scratch_sibling_name` / `stage_split_dir`),
added for issue #224's follow-up: `CloudCorpusStore.seed_workspace` and
`seed.fetch._install_tree` both build on these instead of hand-rolling the
same move-aside/rename/restore/stage dance.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ietf_llm.atomicio import (
    STALE_TMP_SECONDS,
    atomic_open,
    atomic_open_binary,
    scratch_sibling_name,
    stage_split_dir,
    swap_dir,
    swap_dirs,
    sweep_stale_tmp,
)


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


def test_stage_split_dir_preserves_unreplaced_files_and_dirs(tmp_path: Path) -> None:
    live_dir = tmp_path / "index"
    live_dir.mkdir()
    (live_dir / "topics.json").write_text("old-topics")  # not being replaced
    (live_dir / "subdir").mkdir()
    (live_dir / "subdir" / "nested.txt").write_text("nested")  # a subdirectory
    (live_dir / "embeddings.db").write_text("old-db")  # will be replaced

    source = tmp_path / "fetched"
    source.mkdir()
    (source / "embeddings.db").write_text("new-db")

    staged = stage_split_dir(str(live_dir), "seed", str(source), {"embeddings.db"})

    assert Path(staged).is_dir()
    assert (Path(staged) / "topics.json").read_text() == "old-topics"
    assert (Path(staged) / "subdir" / "nested.txt").read_text() == "nested"
    assert (Path(staged) / "embeddings.db").read_text() == "new-db"
    # live_dir itself is untouched -- the caller swaps `staged` into place.
    assert (live_dir / "embeddings.db").read_text() == "old-db"


def test_stage_split_dir_with_no_live_dir_just_moves_new_files(tmp_path: Path) -> None:
    live_dir = tmp_path / "index"  # does not exist yet
    source = tmp_path / "fetched"
    source.mkdir()
    (source / "embeddings.db").write_text("new-db")

    staged = stage_split_dir(str(live_dir), "seed", str(source), {"embeddings.db"})

    assert (Path(staged) / "embeddings.db").read_text() == "new-db"


def test_stage_split_dir_cleans_up_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mid-call failure means stage_split_dir never returns, so a caller
    # (which only learns the staged path from the return value) has no
    # reference to clean it up -- the function must clean up after itself,
    # rather than leaking a scratch dir neither side can find.
    live_dir = tmp_path / "index"
    live_dir.mkdir()
    (live_dir / "topics.json").write_text("old-topics")
    source = tmp_path / "fetched"
    source.mkdir()
    (source / "embeddings.db").write_text("new-db")

    def flaky_copy2(*_a: object, **_k: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr("ietf_llm.atomicio.shutil.copy2", flaky_copy2)
    with pytest.raises(OSError):
        stage_split_dir(str(live_dir), "seed", str(source), {"embeddings.db"})

    # No leaked ".index.seed.<hex>" scratch sibling survives the failure.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".index.seed.")]
    assert leftovers == []


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


def test_swap_dir_double_failure_raises_a_labelled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both the swap-in AND the restore-on-failure fail (e.g. a filesystem
    # condition that outlives a single rename). dest ends up absent -- worse
    # than "half populated" -- so the raised error must say so and point at
    # where the content actually is, rather than surfacing as an unrelated
    # OSError with no trace of the original failure or the recovery path.
    dest = tmp_path / "corpus"
    dest.mkdir()
    (dest / "old.txt").write_text("old")
    new_tree = tmp_path / "new"
    new_tree.mkdir()
    (new_tree / "new.txt").write_text("new")

    real_rename = os.rename

    def flaky(src: str, dst: str) -> None:
        if src == str(new_tree) or os.path.basename(src).startswith(".corpus.old."):
            raise OSError("boom")
        real_rename(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.rename", flaky)
    with pytest.raises(OSError) as excinfo:
        swap_dir(str(dest), str(new_tree))

    message = str(excinfo.value)
    assert "absent" in message
    assert ".corpus.old." in message
    # The original swap-in failure's message survived into the new one.
    assert "boom" in message
    # dest is genuinely gone -- content is only recoverable from the aside.
    assert not dest.exists()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".corpus.old.")]
    assert len(leftovers) == 1
    assert (tmp_path / leftovers[0] / "old.txt").read_text() == "old"


def test_swap_dirs_lands_all_or_reverts_all(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "old.db").write_text("old-index")
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "old.txt").write_text("old-content")

    index_tmp = tmp_path / "index_tmp"
    index_tmp.mkdir()
    (index_tmp / "new.db").write_text("new-index")
    corpus_tmp = tmp_path / "corpus_tmp"
    corpus_tmp.mkdir()
    (corpus_tmp / "new.txt").write_text("new-content")

    swap_dirs([(str(index_dir), str(index_tmp)), (str(corpus_dir), str(corpus_tmp))])

    assert (index_dir / "new.db").read_text() == "new-index"
    assert (corpus_dir / "new.txt").read_text() == "new-content"
    # No leaked scratch siblings on the clean-landing path.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["corpus", "index"]


def test_swap_dirs_unwinds_earlier_swap_when_a_later_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "old.db").write_text("old-index")
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "old.txt").write_text("old-content")

    index_tmp = tmp_path / "index_tmp"
    index_tmp.mkdir()
    (index_tmp / "new.db").write_text("new-index")
    corpus_tmp = tmp_path / "corpus_tmp"
    corpus_tmp.mkdir()
    (corpus_tmp / "new.txt").write_text("new-content")

    real_rename = os.rename

    def flaky(src: str, dst: str) -> None:
        if src == str(corpus_tmp):
            raise OSError("boom")
        real_rename(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.rename", flaky)
    with pytest.raises(OSError):
        swap_dirs([(str(index_dir), str(index_tmp)), (str(corpus_dir), str(corpus_tmp))])

    # The index swap (which landed first) is fully unwound -- never a mix of
    # the new index with the old corpus content.
    assert (index_dir / "old.db").read_text() == "old-index"
    assert (corpus_dir / "old.txt").read_text() == "old-content"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "corpus", "corpus_tmp", "index",
    ]


def test_swap_dirs_names_a_dest_the_unwind_could_not_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A double failure: the second swap fails (triggering unwind), and the
    # first swap's own unwind restore also fails. This must not be a silent
    # `continue` -- the propagated error must name the dest that stayed
    # unrestored, so a caller can surface a real operability signal instead
    # of believing the rollback was clean.
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "old.db").write_text("old-index")
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "old.txt").write_text("old-content")

    index_tmp = tmp_path / "index_tmp"
    index_tmp.mkdir()
    (index_tmp / "new.db").write_text("new-index")
    corpus_tmp = tmp_path / "corpus_tmp"
    corpus_tmp.mkdir()
    (corpus_tmp / "new.txt").write_text("new-content")

    real_rename = os.rename

    def flaky(src: str, dst: str) -> None:
        if src == str(corpus_tmp):
            raise OSError("corpus swap boom")
        if os.path.basename(src).startswith(".index.old."):
            raise OSError("unwind restore boom")
        real_rename(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.rename", flaky)
    with pytest.raises(OSError) as excinfo:
        swap_dirs([(str(index_dir), str(index_tmp)), (str(corpus_dir), str(corpus_tmp))])

    message = str(excinfo.value)
    assert str(index_dir) in message
    assert "corpus swap boom" in message
    assert "unwind restore boom" in message


def test_atomic_open_temp_is_dot_prefixed_and_removed_on_error(tmp_path: Path) -> None:
    target = tmp_path / "doc.txt"
    target.write_text("old")
    seen: list[str] = []
    with pytest.raises(RuntimeError):
        with atomic_open(str(target)) as fh:
            fh.write("partial")
            seen = os.listdir(tmp_path)
            raise RuntimeError("boom")
    temps = [n for n in seen if n != "doc.txt"]
    assert len(temps) == 1
    assert temps[0].startswith(".doc.txt.") and temps[0].endswith(".tmp")
    assert target.read_text() == "old"
    assert os.listdir(tmp_path) == ["doc.txt"]


def test_atomic_open_binary_temp_is_dot_prefixed(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    with atomic_open_binary(str(target)) as fh:
        fh.write(b"x")
        temps = [n for n in os.listdir(tmp_path) if n != "blob.bin"]
        assert len(temps) == 1 and temps[0].startswith(".blob.bin.")
    assert target.read_bytes() == b"x"
    assert os.listdir(tmp_path) == ["blob.bin"]


# --- sweep_stale_tmp --------------------------------------------------------


def _leak(directory: Path, name: str, age: float = 0.0, binary: bool = False) -> Path:
    """Leave the temp a killed writer would, `age` seconds old. The leaked name
    is captured from inside a real writer so it tracks the production format."""
    target = directory / name
    opener = atomic_open_binary if binary else atomic_open
    before = set(os.listdir(directory))
    with opener(str(target)) as fh:
        fh.write(b"partial" if binary else "partial")
        fh.flush()
        (temp_name,) = set(os.listdir(directory)) - before - {name}
        data = (directory / temp_name).read_bytes()
    leaked = directory / temp_name
    leaked.write_bytes(data)
    target.unlink()
    if age:
        past = time.time() - age
        os.utime(leaked, (past, past))
    return leaked


def test_sweep_removes_old_temp_and_keeps_fresh_one(tmp_path: Path) -> None:
    old = _leak(tmp_path, "a.md", age=2 * STALE_TMP_SECONDS)
    fresh = _leak(tmp_path, "b.md", age=60)
    removed, errors = sweep_stale_tmp(str(tmp_path))
    assert removed == [str(old)] and errors == []
    assert not old.exists() and fresh.exists()


def test_sweep_covers_the_binary_writer_and_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "x" / "y").mkdir(parents=True)
    old = _leak(tmp_path / "x" / "y", "1.eml", age=2 * STALE_TMP_SECONDS, binary=True)
    removed, _errors = sweep_stale_tmp(str(tmp_path))
    assert removed == [str(old)]
    assert not old.exists()


def test_sweep_threshold_is_the_age_argument(tmp_path: Path) -> None:
    just_under = _leak(tmp_path, "a", age=STALE_TMP_SECONDS - 120)
    assert sweep_stale_tmp(str(tmp_path)) == ([], [])
    assert just_under.exists()
    assert sweep_stale_tmp(str(tmp_path), max_age=60)[0] == [str(just_under)]


def test_sweep_keeps_ordinary_files_whatever_their_age(tmp_path: Path) -> None:
    keep = [
        tmp_path / "thread.md",
        tmp_path / "notes.tmp",  # ends in .tmp, but is not an atomic_open temp
        tmp_path / ".hidden",
        tmp_path / ".gather.json.tmp",
        tmp_path / "doc.txt.123.4.tmp",  # undotted: not what atomic_open makes now
    ]
    long_ago = time.time() - 30 * 86400
    for path in keep:
        path.write_text("x")
        os.utime(path, (long_ago, long_ago))
    assert sweep_stale_tmp(str(tmp_path)) == ([], [])
    assert all(path.exists() for path in keep)


def test_sweep_ignores_symlinks_and_does_not_follow_them(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _leak(outside, "victim.md", age=2 * STALE_TMP_SECONDS)
    root = tmp_path / "root"
    root.mkdir()
    # A symlink named like a temp, pointing at an old regular file...
    link = root / victim.name
    os.symlink(victim, link)
    # ...and a symlinked directory holding an old temp.
    os.symlink(outside, root / "linked-dir")
    removed, errors = sweep_stale_tmp(str(root))
    assert (removed, errors) == ([], [])
    assert os.path.islink(link) and victim.exists()


def test_sweep_non_recursive_stays_in_the_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    top = _leak(tmp_path, "a", age=2 * STALE_TMP_SECONDS)
    deep = _leak(tmp_path / "sub", "b", age=2 * STALE_TMP_SECONDS)
    removed, _errors = sweep_stale_tmp(str(tmp_path), recursive=False)
    assert removed == [str(top)]
    assert deep.exists()


def test_sweep_missing_root_is_empty_and_unreadable_dir_is_reported(
    tmp_path: Path,
) -> None:
    assert sweep_stale_tmp(str(tmp_path / "absent")) == ([], [])
    if os.geteuid() == 0:
        return  # root reads everything
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        removed, errors = sweep_stale_tmp(str(tmp_path))
    finally:
        locked.chmod(0o700)
    assert removed == [] and len(errors) == 1 and "locked" in errors[0]
