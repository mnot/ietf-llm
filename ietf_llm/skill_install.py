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


def install() -> int:
    """Copy the bundled skill into ~/.claude/skills/<SKILL_NAME>/.

    `--install-claude-skill` is an explicit "I want the bundled
    version here" request from the user, so we always overwrite any
    existing skill at the destination (other than the no-op case
    where the bundled version is already there). If a user has been
    editing the installed skill, they should be backing those edits
    up before running this — same pattern as `npm install` over a
    locally-modified package.

    Returns a shell-style exit code:
      0 — installed, updated, or already up to date
      1 — internal error (bundled skill missing from the wheel)
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
        # Differs (either out-of-date or locally-edited). Overwrite —
        # the user explicitly asked for the bundled version.
        shutil.rmtree(dest)

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    # Don't preserve world-readable bits any more strictly than needed.
    os.chmod(dest, 0o755)
    print(f"Installed skill to {dest}")
    return 0
