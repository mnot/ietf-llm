"""Tests for ietf_llm.skill_install — the --install-skills path.

Multi-harness: the installer copies each bundled skill under
`data/skills/<name>/` into every detected harness's skills dir. The bundled
skills are the two norms skills; the query/routing skill is not bundled (it
lives in mnot/ietf-skill). Tests redirect `_home()` to a sandbox and create
harness marker dirs to simulate which are present.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ietf_llm.cli import skill_install
from ietf_llm.log import Verbosity

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
    for skill in ("ietf-interpreting", "ietf-contributing"):
        assert _installed(home, "claude", skill)
    # The routing skill is no longer bundled, so it is never installed.
    assert not _installed(home, "claude", "ietf-llm")


def test_norms_install_into_every_detected_harness(
    tmp_path: Path, monkeypatch
) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude", "codex", "gemini", "opencode")
    assert skill_install.install_skills() == 0
    for harness in ("claude", "codex", "gemini", "opencode"):
        assert _installed(home, harness, "ietf-interpreting")
        assert _installed(home, harness, "ietf-contributing")
        # No routing skill is bundled or installed anywhere.
        assert not _installed(home, harness, "ietf-llm")


def test_no_routing_skill_is_installed(tmp_path: Path, monkeypatch) -> None:
    # Neither the query skill dir nor a stray ~/.claude/skills/ietf-llm appears.
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
    edited = home / ".claude/skills/ietf-contributing/SKILL.md"
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
    skill_dest = home / ".claude/skills/ietf-contributing"
    (skill_dest / "SKILL.md").write_text("older bundled content")
    manifest = skill_install._read_manifest()
    skill_install._record(manifest, skill_dest, skill_install._tree_hash(skill_dest))
    skill_install._write_manifest(manifest)
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert "older bundled content" not in (skill_dest / "SKILL.md").read_text()
    assert "Updated the installed ietf-contributing skill" in capsys.readouterr().err


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
    edited = home / ".claude/skills/ietf-contributing/SKILL.md"
    edited.write_text("older content, no manifest")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert edited.read_text() == "older content, no manifest"
    assert "--install-skills" in capsys.readouterr().err


def test_sync_is_silent_when_quiet(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    (home / ".claude/skills/ietf-contributing/SKILL.md").write_text("hand-edited")
    skill_install.sync_if_pristine(Verbosity.QUIET)
    assert capsys.readouterr().err == ""


def test_sync_prunes_pristine_orphan(tmp_path: Path, monkeypatch, capsys) -> None:
    # A skill we installed that is no longer bundled (the retired ietf-llm
    # routing skill) is removed on sync when still pristine — no orphan lingers.
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    orphan = home / ".claude/skills/ietf-llm"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("old routing skill")
    manifest = skill_install._read_manifest()
    skill_install._record(manifest, orphan, skill_install._tree_hash(orphan))
    skill_install._write_manifest(manifest)
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert not orphan.exists()
    assert "no longer bundled" in capsys.readouterr().err


def test_sync_keeps_edited_orphan(tmp_path: Path, monkeypatch, capsys) -> None:
    # An orphaned skill with local edits (hash no longer matches what we
    # recorded) is flagged, not deleted.
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    orphan = home / ".claude/skills/ietf-llm"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("original")
    manifest = skill_install._read_manifest()
    skill_install._record(manifest, orphan, skill_install._tree_hash(orphan))
    skill_install._write_manifest(manifest)
    (orphan / "SKILL.md").write_text("user-edited after we recorded it")
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert orphan.exists()
    assert "remove it manually" in capsys.readouterr().err


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


def test_edited_copies_across_harnesses_report_as_one_line(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Driven through `sync_if_pristine` rather than by handing
    `_report_diverged` a list: the aggregation is only worth anything if the
    real path feeds it, and a synthetic list would agree with whatever the
    code did."""
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude", "gemini")
    skill_install.install_skills()
    capsys.readouterr()
    edited = [
        home / _ROOTS[key] / name / "SKILL.md"
        for key in ("claude", "gemini")
        for name in ("ietf-contributing", "ietf-interpreting")
    ]
    for path in edited:
        path.write_text("hand-edited by the user")

    skill_install.sync_if_pristine(Verbosity.STATUS)

    err = capsys.readouterr().err
    assert err.count("\n") == 1, f"expected one line, got:\n{err}"
    # Each skill and each harness named once, not once per combination.
    for token in ("ietf-contributing", "ietf-interpreting", ".claude", ".gemini"):
        assert err.count(token) == 1, f"{token!r} appears more than once"
    assert "--install-skills" in err
    # And nothing was overwritten.
    assert all(p.read_text() == "hand-edited by the user" for p in edited)


def test_a_single_edited_copy_reads_correctly(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The common case. A sentence would need its verb and pronoun to agree
    with the count; this is phrased so it does not."""
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    (home / _ROOTS["claude"] / "ietf-contributing" / "SKILL.md").write_text("edited")

    skill_install.sync_if_pristine(Verbosity.STATUS)

    err = capsys.readouterr().err.strip()
    assert "ietf-contributing" in err
    # No plural-agreement wreckage: no "copies", no "differ", no "them".
    for wrong in ("copies", "differ ", " them"):
        assert wrong not in err, f"{wrong!r} in singular message: {err}"


def test_nothing_is_reported_when_nothing_diverged(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _sandbox(monkeypatch, tmp_path)
    _present(home, "claude")
    skill_install.install_skills()
    capsys.readouterr()
    skill_install.sync_if_pristine(Verbosity.STATUS)
    assert capsys.readouterr().err == ""
