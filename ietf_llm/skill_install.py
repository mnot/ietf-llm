"""Install the bundled Claude skill into ~/.claude/skills/.

The skill ships as package data under `ietf_llm/data/skill/`. The
installer copies it into Claude's user-level skills directory, with
idempotency and a safety check for user edits.

On every CLI gather, `sync_if_pristine()` keeps an already-installed
skill current: it auto-updates the installed copy to the bundled
version *only* when that copy is unchanged since we last wrote it
(tracked by a content manifest), and otherwise just prints a one-line
notice so a user's local edits are never silently clobbered.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .utils import LogLevel, Verbosity, get_cache_dir, log

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


def _tree_hash(root: Path) -> str:
    """Stable SHA-256 over a directory tree's relative paths + contents.

    Deterministic (paths sorted), so the same content always hashes the
    same regardless of filesystem walk order. Used to tell a pristine
    installed skill (unchanged since we wrote it) from a user-edited one.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_path() -> Path:
    """Sidecar recording the hash of the skill content we last installed.

    Lives in ietf-llm's own cache root, NOT inside the skill directory —
    a file under the skill would itself make the installed tree differ
    from the bundled one and break the comparison.
    """
    return Path(get_cache_dir()) / "installed-skill.json"


def _write_manifest(tree_hash: str) -> None:
    """Record `tree_hash` (and the installing version) as the manifest.

    Best-effort: a failed write just means the next run can't prove the
    installed skill is pristine and falls back to notify-only.
    """
    try:
        path = _manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"sha256": tree_hash, "version": __version__}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _read_manifest() -> Optional[Dict[str, Any]]:
    """Return the recorded manifest dict, or None if absent / unreadable."""
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def install() -> int:
    """Copy the bundled skill into ~/.claude/skills/<SKILL_NAME>/.

    `--install-claude-skill` is an explicit "I want the bundled
    version here" request from the user, so we always overwrite any
    existing skill at the destination (other than the no-op case
    where the bundled version is already there). If a user has been
    editing the installed skill, they should be backing those edits
    up before running this — same pattern as `npm install` over a
    locally-modified package.

    Either way we (re)write the content manifest so later CLI gathers
    can recognise the installed copy as pristine and auto-update it.

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
            # Ensure a manifest exists even for a skill first installed by
            # an older release, so future divergence can be classified.
            _write_manifest(_tree_hash(src))
            return 0
        # Differs (either out-of-date or locally-edited). Overwrite —
        # the user explicitly asked for the bundled version.
        shutil.rmtree(dest)

    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    # Don't preserve world-readable bits any more strictly than needed.
    os.chmod(dest, 0o755)
    _write_manifest(_tree_hash(src))
    print(f"Installed skill to {dest}")
    return 0


def sync_if_pristine(verbosity: Verbosity = Verbosity.STATUS) -> None:
    """Keep an already-installed Claude skill in sync (CLI gathers only).

    Best-effort and never fatal — a UX nudge, like the cache-staleness
    banner. Does nothing when the skill isn't installed (we never install
    a skill the user didn't ask for) or is already current. When the
    installed copy differs from the bundled one:

      - pristine (unchanged since we last wrote it, per the manifest) →
        silently update it to the bundled version;
      - otherwise (local edits, or installed by a release predating the
        manifest) → print a one-line notice prompting an explicit
        `--install-claude-skill`, so edits are never clobbered silently.
    """
    try:
        _sync_if_pristine(verbosity)
    except Exception:  # pylint: disable=broad-except
        # Purely a convenience nudge; any failure (permissions, a weird
        # skills dir, a read error) must not derail the gather it follows.
        pass


def _sync_if_pristine(verbosity: Verbosity) -> None:
    src = _bundled_root()
    dest = DEST_ROOT / SKILL_NAME
    if not src.is_dir() or not dest.is_dir():
        return  # nothing bundled, or skill not installed — leave it alone

    bundled = _tree_hash(src)
    installed = _tree_hash(dest)
    if installed == bundled:
        # Already current. Backfill the manifest if it's missing or stale
        # so a future divergence is classifiable.
        manifest = _read_manifest()
        if not manifest or manifest.get("sha256") != bundled:
            _write_manifest(bundled)
        return

    manifest = _read_manifest()
    pristine = manifest is not None and manifest.get("sha256") == installed
    if pristine:
        # Copy into a temp sibling and swap atomically rather than rmtree-ing
        # the live skill before copying: a failure mid-copy must never leave
        # the installed skill destroyed.
        staged = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
        retired = dest.with_name(f"{dest.name}.old-{os.getpid()}")
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(src, staged)
        os.chmod(staged, 0o755)
        os.replace(dest, retired)
        try:
            os.replace(staged, dest)
        except OSError:
            os.replace(retired, dest)  # roll back to the original on failure
            raise
        shutil.rmtree(retired, ignore_errors=True)
        _write_manifest(bundled)
        log(
            f"Updated the installed ietf-llm Claude skill at {dest} "
            f"to match ietf-llm {__version__}.",
            verbosity,
            level=LogLevel.STATUS,
        )
    else:
        log(
            f"The installed ietf-llm Claude skill at {dest} differs from "
            "the bundled version (local edits, or installed by an older "
            "release). Run `ietf-llm --install-claude-skill` to update it "
            "(overwrites the installed copy).",
            verbosity,
            level=LogLevel.STATUS,
        )
