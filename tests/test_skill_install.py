"""Tests for ietf_llm.skill_install — the --install-claude-skill path.

Verifies the advertised behaviours:
- fresh install: copies and returns 0
- already up to date: no-op, returns 0
- destination modified: overwritten (the user asked us to install)
- bundled skill missing from the wheel: clean error, returns 1
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ietf_llm import skill_install
from ietf_llm.utils import Verbosity


def _redirect_dest(monkeypatch, dest_root: Path) -> Path:
    """Point skill_install at a sandbox dest instead of ~/.claude/skills/."""
    monkeypatch.setattr(skill_install, "DEST_ROOT", dest_root)
    return dest_root / skill_install.SKILL_NAME


def _redirect_manifest(monkeypatch, cache_root: Path) -> Path:
    """Point the install manifest at a sandbox cache root."""
    cache_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skill_install, "get_cache_dir", lambda: str(cache_root))
    return cache_root / "installed-skill.json"


def test_fresh_install_copies_skill(tmp_path: Path, monkeypatch) -> None:
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    rc = skill_install.install()
    assert rc == 0
    assert dest.is_dir()
    assert (dest / "SKILL.md").exists()


def test_install_idempotent_when_identical(tmp_path: Path, monkeypatch) -> None:
    _redirect_dest(monkeypatch, tmp_path / "skills")
    assert skill_install.install() == 0
    # Second call should detect identical content and no-op cleanly.
    assert skill_install.install() == 0


def test_install_overwrites_modified_destination(
    tmp_path: Path, monkeypatch
) -> None:
    # `--install-claude-skill` is an explicit "I want the bundled
    # version" request; overwrite without ceremony. (The user can back
    # up their local edits beforehand if they care.)
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    skill_install.install()
    (dest / "SKILL.md").write_text("user-edited content")
    rc = skill_install.install()
    assert rc == 0
    assert "user-edited content" not in (dest / "SKILL.md").read_text()


def test_install_handles_missing_source(tmp_path: Path, monkeypatch, capsys) -> None:
    _redirect_dest(monkeypatch, tmp_path / "skills")
    with patch.object(
        skill_install, "_bundled_root", return_value=tmp_path / "nope"
    ):
        rc = skill_install.install()
    assert rc == 1
    err = capsys.readouterr().err
    assert "bundled skill not found" in err


# --- sync_if_pristine -----------------------------------------------------


def test_sync_noop_when_not_installed(tmp_path: Path, monkeypatch, capsys) -> None:
    # No skill at the destination — never install one the user didn't ask for.
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert not dest.exists()
    assert capsys.readouterr().err == ""


def test_sync_noop_when_up_to_date(tmp_path: Path, monkeypatch, capsys) -> None:
    _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    capsys.readouterr()  # drop the install line
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert capsys.readouterr().err == ""


def test_sync_auto_updates_pristine_skill(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # A skill we installed, left untouched, but now out of date relative
    # to the bundled content: silently brought up to date.
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    capsys.readouterr()
    # Simulate the bundled content moving on by mutating the installed
    # copy *and* recording its current hash as the manifest — i.e. the
    # installed tree is exactly what we last wrote, just older content.
    (dest / "SKILL.md").write_text("older bundled content")
    skill_install._write_manifest(skill_install._tree_hash(dest))
    skill_install.sync_if_pristine(Verbosity.STATUS)
    # Restored to the real bundled SKILL.md, and a notice was printed.
    assert "older bundled content" not in (dest / "SKILL.md").read_text()
    assert "Updated the installed ietf-llm Claude skill" in capsys.readouterr().err


def test_sync_notifies_when_user_edited(tmp_path: Path, monkeypatch, capsys) -> None:
    # Installed copy diverges from BOTH the bundled content and the
    # manifest → treated as user-edited; notify, don't clobber.
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    capsys.readouterr()
    (dest / "SKILL.md").write_text("hand-edited by the user")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    # Left as-is; the notice points at the explicit install command.
    assert (dest / "SKILL.md").read_text() == "hand-edited by the user"
    err = capsys.readouterr().err
    assert "--install-claude-skill" in err


def test_sync_notifies_when_no_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    # Out-of-date and no manifest (installed by an older release): we
    # can't prove it's pristine, so notify rather than clobber.
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    manifest = _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    manifest.unlink()
    capsys.readouterr()
    (dest / "SKILL.md").write_text("older content, no manifest")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert (dest / "SKILL.md").read_text() == "older content, no manifest"
    assert "--install-claude-skill" in capsys.readouterr().err


def test_sync_is_silent_when_quiet(tmp_path: Path, monkeypatch, capsys) -> None:
    dest = _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    capsys.readouterr()
    (dest / "SKILL.md").write_text("hand-edited by the user")
    skill_install.sync_if_pristine(Verbosity.QUIET)
    assert capsys.readouterr().err == ""


def test_sync_never_raises(tmp_path: Path, monkeypatch) -> None:
    # Best-effort: a failure deep in the check must not propagate.
    _redirect_dest(monkeypatch, tmp_path / "skills")
    _redirect_manifest(monkeypatch, tmp_path / "cache")
    skill_install.install()
    with patch.object(skill_install, "_tree_hash", side_effect=OSError("boom")):
        skill_install.sync_if_pristine(Verbosity.STATUS)  # no exception


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
