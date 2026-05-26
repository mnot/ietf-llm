"""Tests for ietf_llm.skill_install — the --install-claude-skill path.

Verifies the four advertised behaviours:
- fresh install: copies and returns 0
- already up to date: no-op, returns 0
- destination modified: refuses, returns 1
- --force with modification: overwrites, returns 0
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ietf_llm import skill_install


def _redirect_dest(monkeypatch, dest_root: Path) -> Path:
    """Point skill_install at a sandbox dest instead of ~/.claude/skills/."""
    monkeypatch.setattr(skill_install, "DEST_ROOT", dest_root)
    return dest_root / skill_install.SKILL_NAME


def test_fresh_install_copies_skill(tmp_path: Path, monkeypatch) -> None:
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    rc = skill_install.install(force=False)
    assert rc == 0
    assert dest.is_dir()
    assert (dest / "SKILL.md").exists()


def test_install_idempotent_when_identical(tmp_path: Path, monkeypatch) -> None:
    _redirect_dest(monkeypatch, tmp_path / "skills")
    assert skill_install.install(force=False) == 0
    # Second call should detect identical content and no-op cleanly.
    assert skill_install.install(force=False) == 0


def test_install_refuses_when_destination_modified(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    skill_install.install(force=False)
    # Simulate the user editing the skill.
    (dest / "SKILL.md").write_text("user-edited content")
    rc = skill_install.install(force=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert "--force" in err


def test_install_force_overwrites_modification(
    tmp_path: Path, monkeypatch
) -> None:
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    skill_install.install(force=False)
    (dest / "SKILL.md").write_text("user-edited content")
    rc = skill_install.install(force=True)
    assert rc == 0
    assert "user-edited content" not in (dest / "SKILL.md").read_text()


def test_install_handles_missing_source(tmp_path: Path, monkeypatch, capsys) -> None:
    _redirect_dest(monkeypatch, tmp_path / "skills")
    with patch.object(
        skill_install, "_bundled_root", return_value=tmp_path / "nope"
    ):
        rc = skill_install.install(force=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert "bundled skill not found" in err


# --- _dirs_identical ------------------------------------------------------


def test_dirs_identical_positive(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("same")
    (b / "f.txt").write_text("same")
    assert skill_install._dirs_identical(a, b) is True


def test_dirs_identical_content_differs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("one")
    (b / "f.txt").write_text("two")
    assert skill_install._dirs_identical(a, b) is False


def test_dirs_identical_file_only_on_one_side(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("x")
    (b / "f.txt").write_text("x")
    (a / "extra.txt").write_text("only-on-a")
    assert skill_install._dirs_identical(a, b) is False


def test_dirs_identical_recurses_into_subdirs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "sub").mkdir(parents=True)
    (b / "sub").mkdir(parents=True)
    (a / "sub" / "x.txt").write_text("same")
    (b / "sub" / "x.txt").write_text("same")
    assert skill_install._dirs_identical(a, b) is True
    (b / "sub" / "x.txt").write_text("different")
    assert skill_install._dirs_identical(a, b) is False
