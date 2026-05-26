"""Install the bundled Claude skill into ~/.claude/skills/.

The skill ships as package data under `ietf_llm/data/skill/`. The
installer copies it into Claude's user-level skills directory, with
idempotency and a safety check for user edits.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

SKILL_NAME = "ietf-llm"
DEST_ROOT = Path("~/.claude/skills").expanduser()


def _bundled_root() -> Path:
    """Return the on-disk path of the bundled skill directory."""
    # importlib.resources gives an abstract Traversable; for a real
    # directory we need it as a Path. `as_file` returns a context
    # manager; since the skill ships unpacked in the wheel via
    # package-data + include_package_data, the path is stable and we
    # can use it directly.
    return Path(str(resources.files("ietf_llm").joinpath("data/skill")))


def _dirs_identical(left: Path, right: Path) -> bool:
    """True if two directory trees have identical file contents."""
    cmp = filecmp.dircmp(str(left), str(right))
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    for sub in cmp.common_dirs:
        if not _dirs_identical(left / sub, right / sub):
            return False
    return True


def install(force: bool = False) -> int:
    """Copy the bundled skill into ~/.claude/skills/<SKILL_NAME>/.

    Returns a shell-style exit code:
      0 — installed, updated, or already up to date
      1 — refused because destination has user modifications (no --force)
    """
    src = _bundled_root()
    if not src.is_dir():
        print(
            f"Internal error: bundled skill not found at {src}. "
            "Try reinstalling: pipx install --force ietf-llm",
            file=sys.stderr,
        )
        return 1

    dest = DEST_ROOT / SKILL_NAME

    if dest.exists():
        if _dirs_identical(src, dest):
            print(f"Skill already installed and up to date at {dest}.")
            return 0
        if not force:
            print(
                f"Skill at {dest} differs from the bundled version "
                "(either out of date or locally edited).\n"
                "Re-run with --force to overwrite, or back it up first.",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(dest)

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    # Don't preserve world-readable bits any more strictly than needed.
    os.chmod(dest, 0o755)
    print(f"Installed skill to {dest}")
    return 0
