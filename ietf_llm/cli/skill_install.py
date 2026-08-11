"""Install the bundled Agent Skills into every supported agent harness.

The skills ship as package data under `ietf_llm/data/skills/<name>/`, each a
self-contained Agent Skill (`SKILL.md` with `name` + `description`
frontmatter — the open standard at agentskills.io). Two are bundled, both
norms skills vendored from mnot/ietf-skill (see data/skills/VENDORED.md):

  - `ietf-interpreting`  — read-side norms (consensus, attribution, …)
  - `ietf-contributing`  — write-side norms (drafting list mail / issues)

There is no bundled query/routing skill — routing comes from the MCP server's
`instructions` field (see `data/mcp-instructions.md`), served to every client.

`--install-skills` detects every supported harness present on the machine
(Claude Code, Codex, Gemini CLI, opencode — all adopters of the Agent Skills
open standard) and installs each bundled (norms) skill into every one's skills
directory, with idempotency and a safety check for user edits. It is a
convenience: the two norms skills are vendored copies of what mnot/ietf-skill
publishes, so installing them from that repo instead is equivalent.

On every CLI gather, `sync_if_pristine()` keeps already-installed skills
current: it auto-updates an installed copy to the bundled version *only* when
that copy is unchanged since we last wrote it (tracked by a content
manifest), and otherwise just prints a one-line notice so a user's local
edits are never silently clobbered.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List

from .. import __version__
from ..log import LogLevel, Verbosity, log
from ..paths import get_cache_dir


@dataclass(frozen=True)
class Harness:
    """A supported agent harness and where it discovers user-level skills."""

    key: str  # short id: "claude" / "codex" / "gemini" / "opencode"
    label: str  # human label for output
    marker: Path  # config dir whose existence means the harness is installed
    skills_root: Path  # where to install skills for this harness


def _home() -> Path:
    """User home — indirected so tests can redirect the whole harness table."""
    return Path.home()


def _harnesses() -> List[Harness]:
    """Supported harnesses and their skill-discovery paths, per each tool's
    current docs (Agent Skills open standard). Codex reads `~/.agents/skills`,
    Gemini `~/.gemini/skills`, opencode `~/.config/opencode/skills` (and also
    `~/.claude/skills`), Claude `~/.claude/skills`."""
    home = _home()
    return [
        Harness("claude", "Claude Code", home / ".claude", home / ".claude" / "skills"),
        Harness("codex", "Codex CLI", home / ".codex", home / ".agents" / "skills"),
        Harness("gemini", "Gemini CLI", home / ".gemini", home / ".gemini" / "skills"),
        Harness(
            "opencode",
            "opencode",
            home / ".config" / "opencode",
            home / ".config" / "opencode" / "skills",
        ),
    ]


def _detect_harnesses() -> List[Harness]:
    """Harnesses present on this machine (their config dir exists)."""
    return [h for h in _harnesses() if h.marker.is_dir()]


def _bundled_skills_root() -> Path:
    """On-disk path of the bundled skills parent directory (`data/skills/`)."""
    return Path(str(resources.files("ietf_llm").joinpath("data/skills")))


def _bundled_skills() -> List[Path]:
    """Each bundled skill's source directory (one per `SKILL.md`), sorted."""
    root = _bundled_skills_root()
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if (d / "SKILL.md").is_file())


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

    Deterministic (paths sorted), so the same content always hashes the same
    regardless of filesystem walk order. Used to tell a pristine installed
    skill (unchanged since we wrote it) from a user-edited one.
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
    """Sidecar recording the hash of each skill we last installed, keyed by its
    absolute destination path (so the same skill installed into several
    harnesses tracks independently).

    Lives in ietf-llm's own cache root, NOT inside any skill directory — a file
    under a skill would itself make the installed tree differ from the bundled
    one and break the comparison.
    """
    return Path(get_cache_dir()) / "installed-skill.json"


def _read_manifest() -> Dict[str, Any]:
    """Return the manifest map `{dest_path: {sha256, version}}`, or `{}`.

    Tolerates the legacy single-skill format (a bare `{sha256, version}`): it
    has no `skills` map, so per-dest lookups miss and the affected skill is
    treated conservatively (notify, don't clobber).
    """
    try:
        data = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    skills = data.get("skills")
    return skills if isinstance(skills, dict) else {}


def _write_manifest(manifest: Dict[str, Any]) -> None:
    """Persist the manifest map. Best-effort: a failed write just means the
    next run can't prove an install is pristine and falls back to notify-only.
    """
    try:
        path = _manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"skills": manifest, "version": __version__}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _record(manifest: Dict[str, Any], dest: Path, tree_hash: str) -> None:
    """Set the manifest entry for one destination."""
    manifest[str(dest)] = {"sha256": tree_hash, "version": __version__}


def _copy_skill(src: Path, dest: Path) -> None:
    """Overwrite `dest` with `src` (the explicit-install path)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    os.chmod(dest, 0o755)


def _install_into(root: Path, skills: List[Path]) -> None:
    """Install each of `skills` into `root/<name>/`, overwriting as needed (an
    explicit install means "I want the bundled version here"). Updates the
    manifest.

    Idempotent per skill: an already-identical destination is left untouched
    (but its manifest entry is refreshed so future divergence is classifiable).
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest()
    for src in skills:
        dest = root / src.name
        if dest.exists() and _dirs_identical(src, dest):
            _record(manifest, dest, _tree_hash(src))
            continue
        _copy_skill(src, dest)
        _record(manifest, dest, _tree_hash(src))
    _write_manifest(manifest)


def install_skills() -> int:
    """Install the bundled skills into every supported harness present on this
    machine (`--install-skills`).

    Every bundled (norms) skill goes into each detected harness's own skills
    dir. Overwrites any existing copy at each destination (explicit install) —
    a user's edits are restored too, so back them up first if that matters.

    Returns a shell-style exit code:
      0 — installed into the detected harnesses (or none detected: nothing to do)
      1 — internal error (bundled skills missing from the wheel)
    """
    bundled = _bundled_skills()
    if not bundled:
        print(
            f"Internal error: bundled skills not found at "
            f"{_bundled_skills_root()}. "
            "Try reinstalling: pipx install --force ietf-llm",
            file=sys.stderr,
        )
        return 1

    present = _detect_harnesses()
    if not present:
        print(
            "No supported agent harness detected (looked for ~/.claude, "
            "~/.codex, ~/.gemini, ~/.config/opencode). Nothing installed."
        )
        return 0

    # Group skills by destination root so a root shared by two harnesses is
    # written once. Every bundled skill (all norms) goes into each harness.
    by_root: Dict[Path, Dict[str, Path]] = {}
    for harness in present:
        dest = by_root.setdefault(harness.skills_root, {})
        for skill in bundled:
            dest[skill.name] = skill

    for root, skills in by_root.items():
        _install_into(root, list(skills.values()))

    print("Installed skills into: " + ", ".join(h.label for h in present) + ".")
    for root in sorted(by_root, key=str):
        names = ", ".join(sorted(by_root[root]))
        print(f"  {names} → {root}")
    print(
        "  (a convenience copy of the norms skills from mnot/ietf-skill; "
        "routing itself comes from the MCP server's instructions.)"
    )
    return 0


def sync_if_pristine(verbosity: Verbosity = Verbosity.STATUS) -> None:
    """Keep already-installed skills in sync (CLI gathers only).

    Best-effort and never fatal — a UX nudge, like the cache-staleness banner.
    Only touches skills already installed (we never install a skill the user
    didn't ask for). When an installed copy differs from the bundled one:

      - pristine (unchanged since we last wrote it, per the manifest) →
        silently update it to the bundled version;
      - otherwise (local edits, or installed by a release predating the
        manifest) → print a one-line notice prompting an explicit
        `--install-skills`, so edits are never clobbered silently.
    """
    try:
        _sync_if_pristine(verbosity)
    except Exception:  # pylint: disable=broad-except
        # Purely a convenience nudge; any failure (permissions, a weird skills
        # dir, a read error) must not derail the gather it follows.
        pass


def _sync_if_pristine(verbosity: Verbosity) -> None:
    bundled = {s.name: s for s in _bundled_skills()}
    if not bundled:
        return
    manifest = _read_manifest()
    changed = False
    # Every harness's own skills root.
    roots = {h.skills_root for h in _harnesses()}
    for root in roots:
        for name, src in bundled.items():
            dest = root / name
            if not dest.is_dir():
                continue  # not installed here — leave it alone
            if _sync_one(src, dest, manifest, verbosity):
                changed = True
    if _prune_orphans(manifest, set(bundled), verbosity):
        changed = True
    if changed:
        _write_manifest(manifest)


def _prune_orphans(
    manifest: Dict[str, Any], bundled_names: "set[str]", verbosity: Verbosity
) -> bool:
    """Remove skills we previously installed that are no longer bundled — e.g.
    the retired `ietf-llm` routing skill — when the installed copy is still
    pristine, so an upgrade does not leave stale guidance behind now that
    routing is served from the MCP `instructions` field. A user-edited copy
    is flagged, not deleted. Only touches destinations we recorded in the
    manifest (i.e. that we wrote). Mutates `manifest`; returns True if changed.
    """
    changed = False
    for dest_str in list(manifest):
        if Path(dest_str).name in bundled_names:
            continue  # still bundled — the normal sync handles it
        dest = Path(dest_str)
        if not dest.is_dir():
            manifest.pop(dest_str, None)  # already gone — drop the stale entry
            changed = True
            continue
        recorded = manifest.get(dest_str, {}).get("sha256")
        if recorded is not None and _tree_hash(dest) == recorded:
            shutil.rmtree(dest, ignore_errors=True)
            manifest.pop(dest_str, None)
            changed = True
            log(
                f"Removed the obsolete {dest.name} skill at {dest} — it is no "
                "longer bundled (its guidance is now served by the MCP server).",
                verbosity,
                level=LogLevel.STATUS,
            )
        else:
            log(
                f"The installed {dest.name} skill at {dest} is no longer bundled "
                "but has local edits; remove it manually if you no longer want it.",
                verbosity,
                level=LogLevel.STATUS,
            )
    return changed


def _sync_one(
    src: Path, dest: Path, manifest: Dict[str, Any], verbosity: Verbosity
) -> bool:
    """Sync one installed skill against its bundled source. Mutates `manifest`
    in place; returns True if the manifest changed (so the caller persists)."""
    bundled = _tree_hash(src)
    installed = _tree_hash(dest)
    if installed == bundled:
        # Already current. Backfill the manifest if missing/stale so a future
        # divergence is classifiable.
        entry = manifest.get(str(dest))
        if not entry or entry.get("sha256") != bundled:
            _record(manifest, dest, bundled)
            return True
        return False

    entry = manifest.get(str(dest))
    pristine = entry is not None and entry.get("sha256") == installed
    if not pristine:
        log(
            f"The installed {src.name} skill at {dest} differs from the "
            "bundled version (local edits, or installed by an older release). "
            "Run `ietf-llm --install-skills` to update it (overwrites the "
            "installed copy).",
            verbosity,
            level=LogLevel.STATUS,
        )
        return False

    # Pristine but out of date: copy into a temp sibling and swap atomically
    # rather than rmtree-ing the live skill before copying — a failure mid-copy
    # must never leave the installed skill destroyed.
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
    _record(manifest, dest, bundled)
    log(
        f"Updated the installed {src.name} skill at {dest} "
        f"to match ietf-llm {__version__}.",
        verbosity,
        level=LogLevel.STATUS,
    )
    return True
