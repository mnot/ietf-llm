"""Tests for ietf_llm.skill_install — the --install-skills path.

Multi-harness: the installer copies each bundled skill under
`data/skills/<name>/` into every detected harness's skills dir. The norms
skills go into every detected harness; the query skill (`ietf-llm`) goes only
into `~/.claude/skills/` (Claude + opencode read it). Tests redirect `_home()`
to a sandbox and create harness marker dirs to simulate which are present.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ietf_llm import skill_install
from ietf_llm.utils import Verbosity

_MARKERS = {
    "claude": ".claude",
    "codex": ".codex",
    "gemini": ".gemini",
    "opencode": ".config/opencode",
}
# Where each harness discovers skills (relative to home).
_ROOTS = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
    "gemini": ".gemini/skills",
    "opencode": ".config/opencode/skills",
}


def _sandbox(monkeypatch, tmp_path: Path) -> Path:
    """Redirect home + cache to a sandbox; return the sandbox home."""
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(skill_install, "_home", lambda: home)
    monkeypatch.setattr(skill_install, "get_cache_dir", lambda: str(tmp_path / "cache"))
    return home


def _present(home: Path, *keys: str) -> None:
    """Create marker dirs so those harnesses count as installed."""
    for key in keys:
        (home / _MARKERS[key]).mkdir(parents=True, exist_ok=True)


def _installed(home: Path, harness: str, skill: str) -> bool:
    return (home / _ROOTS[harness] / skill / "SKILL.md").exists()


# --- install_skills -------------------------------------------------------


def test_installs_all_skills_into_claude(tmp_path: Path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    assert skill_install.install_skills() == 0
    for skill in ("ietf-llm", "ietf-interpreting", "ietf-contributing"):
        assert _installed(home, "claude", skill)


def test_norms_everywhere_query_claude_only(tmp_path: Path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude", "codex", "gemini")
    assert skill_install.install_skills() == 0
    # Norms land in every detected harness's own root.
    for harness in ("claude", "codex", "gemini"):
        assert _installed(home, harness, "ietf-interpreting")
        assert _installed(home, harness, "ietf-contributing")
    # Query skill only in ~/.claude/skills — NOT in Codex/Gemini dirs.
    assert _installed(home, "claude", "ietf-llm")
    assert not _installed(home, "codex", "ietf-llm")
    assert not _installed(home, "gemini", "ietf-llm")


def test_query_reaches_claude_dir_for_opencode_only(
    tmp_path: Path, monkeypatch
) -> None:
    # opencode reads ~/.claude/skills, so the query skill goes there even
    # when Claude itself isn't installed.
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "opencode")
    assert skill_install.install_skills() == 0
    assert _installed(home, "opencode", "ietf-interpreting")
    assert _installed(home, "opencode", "ietf-contributing")
    # Query skill not in opencode's own dir, but in ~/.claude/skills.
    assert not _installed(home, "opencode", "ietf-llm")
    assert (home / ".claude/skills/ietf-llm/SKILL.md").exists()


def test_no_query_skill_when_only_codex_gemini(
    tmp_path: Path, monkeypatch
) -> None:
    # Neither Claude nor opencode present → the query skill has no skill home
    # (those harnesses get routing from the MCP instructions field instead).
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "codex", "gemini")
    assert skill_install.install_skills() == 0
    assert _installed(home, "codex", "ietf-interpreting")
    assert not (home / ".claude/skills/ietf-llm").exists()


def test_no_harness_detected_installs_nothing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    rc = skill_install.install_skills()
    assert rc == 0
    assert "No supported agent harness detected" in capsys.readouterr().out
    assert not (home / ".claude").exists()


def test_install_idempotent(tmp_path: Path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    assert skill_install.install_skills() == 0
    assert skill_install.install_skills() == 0


def test_install_overwrites_modified(tmp_path: Path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    edited = home / ".claude/skills/ietf-llm/SKILL.md"
    edited.write_text("user-edited content")
    assert skill_install.install_skills() == 0
    assert "user-edited content" not in edited.read_text()


def test_missing_bundled_skills(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    with patch.object(
        skill_install, "_bundled_skills_root", return_value=tmp_path / "nope"
    ):
        rc = skill_install.install_skills()
    assert rc == 1
    assert "bundled skills not found" in capsys.readouterr().err


# --- sync_if_pristine -----------------------------------------------------


def test_sync_noop_when_not_installed(tmp_path: Path, monkeypatch, capsys) -> None:
    _sandbox(monkeypatch, tmp_path)
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert capsys.readouterr().err == ""


def test_sync_noop_when_up_to_date(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert capsys.readouterr().err == ""


def test_sync_auto_updates_pristine_skill(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    skill_dest = home / ".claude/skills/ietf-llm"
    (skill_dest / "SKILL.md").write_text("older bundled content")
    manifest = skill_install._read_manifest()
    skill_install._record(manifest, skill_dest, skill_install._tree_hash(skill_dest))
    skill_install._write_manifest(manifest)
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert "older bundled content" not in (skill_dest / "SKILL.md").read_text()
    assert "Updated the installed ietf-llm skill" in capsys.readouterr().err


def test_sync_notifies_when_user_edited(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    edited = home / ".claude/skills/ietf-contributing/SKILL.md"
    edited.write_text("hand-edited by the user")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert edited.read_text() == "hand-edited by the user"
    assert "--install-skills" in capsys.readouterr().err


def test_sync_notifies_when_no_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    skill_install._manifest_path().unlink()
    capsys.readouterr()
    edited = home / ".claude/skills/ietf-llm/SKILL.md"
    edited.write_text("older content, no manifest")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert edited.read_text() == "older content, no manifest"
    assert "--install-skills" in capsys.readouterr().err


def test_sync_is_silent_when_quiet(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    (home / ".claude/skills/ietf-llm/SKILL.md").write_text("hand-edited")
    skill_install.sync_if_pristine(Verbosity.QUIET)
    assert capsys.readouterr().err == ""


def test_sync_never_raises(tmp_path: Path, monkeypatch) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    with patch.object(skill_install, "_tree_hash", side_effect=OSError("boom")):
        skill_install.sync_if_pristine(Verbosity.STATUS)  # no exception


# --- _dirs_identical ------------------------------------------------------


def test_dirs_identical_positive(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("same")
    (b / "f.txt").write_text("same")
    assert skill_install._dirs_identical(a, b) is True


def test_dirs_identical_content_differs(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("one")
    (b / "f.txt").write_text("two")
    assert skill_install._dirs_identical(a, b) is False


def test_dirs_identical_file_only_on_one_side(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("x")
    (b / "f.txt").write_text("x")
    (a / "extra.txt").write_text("only-on-a")
    assert skill_install._dirs_identical(a, b) is False


def test_dirs_identical_recurses_into_subdirs(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sub").mkdir(parents=True)
    (b / "sub").mkdir(parents=True)
    (a / "sub" / "x.txt").write_text("same")
    (b / "sub" / "x.txt").write_text("same")
    assert skill_install._dirs_identical(a, b) is True
    (b / "sub" / "x.txt").write_text("different")
    assert skill_install._dirs_identical(a, b) is False
