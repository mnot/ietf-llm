"""Install the bundled Agent Skills into every supported agent harness.

The skills ship as package data under `ietf_llm/data/skills/<name>/`, each a
self-contained Agent Skill (`SKILL.md` with `name` + `description`
frontmatter — the open standard at agentskills.io), vendored wholesale from
mnot/ietf-skill (see data/skills/VENDORED.md). Whatever that repo publishes is
what ships — the set is discovered when re-vendoring, not listed anywhere here,
so nothing in this module needs touching when it changes.

There is no bundled query/routing skill — routing comes from the MCP server's
`instructions` field (see `data/mcp-instructions.md`), served to every client.

`--install-skills` detects every supported harness present on the machine
(Claude Code, Codex, Gemini CLI, opencode — all adopters of the Agent Skills
open standard) and installs each bundled skill into every one's skills
directory, with idempotency and a safety check for user edits. It is a
convenience: they are vendored copies of what mnot/ietf-skill publishes, so
installing them from that repo instead is equivalent.

Inside WSL, also detects the same harnesses installed on the Windows side
(`_wsl_windows_home()`) — WSL and Windows don't share a home directory, so a
WSL-installed `ietf-llm` would otherwise have no way to reach a Windows-native
Claude Code/Codex/Gemini/opencode install. Note that this only installs the
*skills* there; registering the MCP server with a Windows-side harness needs a
`wsl.exe` wrapper in that harness's own config (see docs/mcp-local.md).

On every CLI gather, `sync_if_pristine()` keeps already-installed skills
current: it auto-updates an installed copy to the bundled version *only* when
that copy is unchanged since we last wrote it (tracked by a content
manifest), and otherwise just prints a one-line notice so a user's local
edits are never silently clobbered. That path deliberately does not probe for
a Windows-side home — it reads the roots it already knows from the manifest,
so a gather never pays the WSL interop cold start.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _is_wsl() -> bool:
    """True inside a WSL distro (not on native Windows or native Linux)."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _wsl_windows_home() -> Optional[Path]:
    """The Windows-side home directory, when running inside WSL.

    A harness installed on the Windows side (e.g. Claude Code run from a
    Windows terminal, not the WSL one) keeps its own `~/.claude` etc. under
    the Windows home, which is invisible to the Linux-side `_home()` we
    otherwise use — WSL and Windows are separate filesystems with separate
    homes. Windows' own interop (`cmd.exe`, `wslpath`) is how we ask it,
    since there is no other route from inside the distro. Best-effort: any
    failure (interop disabled, `cmd.exe`/`wslpath` missing, timeout) just
    means we don't detect a Windows-side install, not a crash.

    Two subprocesses with a cold start to pay, so only the explicit
    `--install-skills` path calls this; the per-gather `sync_if_pristine`
    takes its Windows-side roots from the manifest instead.
    """
    if not _is_wsl():
        return None
    try:
        userprofile = _run_last_line(["cmd.exe", "/c", "echo %USERPROFILE%"])
        if not userprofile or "%USERPROFILE%" in userprofile:
            return None
        converted = _run_last_line(["wslpath", "-u", userprofile])
        if not converted:
            return None
        path = Path(converted)
        return path if path.is_dir() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _run_last_line(cmd: List[str]) -> str:
    """Run `cmd`, returning the last line of its stdout stripped (`""` if none).

    `cmd.exe` invoked with a Linux working directory prints a banner ("UNC
    paths are not supported...") before running the command. Measured on
    Ubuntu/WSL2 (see #244) that goes to stderr, which we capture and discard
    — but taking the last stdout line rather than the whole blob costs
    nothing and keeps us right if some other build sends it to stdout.
    """
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    ).stdout.strip()
    lines = out.splitlines()
    return lines[-1].strip() if lines else ""


def _harnesses_for(
    home: Path, key_suffix: str = "", label_suffix: str = ""
) -> List[Harness]:
    """The harness table rooted at `home`, per each tool's current docs (Agent
    Skills open standard). Codex reads `~/.agents/skills`, Gemini
    `~/.gemini/skills`, opencode `~/.config/opencode/skills` (and also
    `~/.claude/skills`), Claude `~/.claude/skills`.

    Used for both the Linux-side table (`_home()`, no suffix) and the
    Windows-side one under WSL (`_wsl_windows_home()`, `-win`/` (Windows)`) —
    building each fresh from its own root avoids reconstructing one from the
    other via `Path.relative_to`, which would raise if a future harness ever
    lived outside `home`.
    """
    return [
        Harness(
            f"claude{key_suffix}",
            f"Claude Code{label_suffix}",
            home / ".claude",
            home / ".claude" / "skills",
        ),
        Harness(
            f"codex{key_suffix}",
            f"Codex CLI{label_suffix}",
            home / ".codex",
            home / ".agents" / "skills",
        ),
        Harness(
            f"gemini{key_suffix}",
            f"Gemini CLI{label_suffix}",
            home / ".gemini",
            home / ".gemini" / "skills",
        ),
        Harness(
            f"opencode{key_suffix}",
            f"opencode{label_suffix}",
            home / ".config" / "opencode",
            home / ".config" / "opencode" / "skills",
        ),
    ]


def _harnesses() -> List[Harness]:
    """Supported harnesses and their skill-discovery paths.

    Inside WSL, also looks for the same harnesses installed on the Windows
    side (see `_wsl_windows_home()`) — a WSL-installed `ietf-llm` otherwise
    has no way to reach a Windows-native Claude Code/Codex/Gemini/opencode
    install, since the two sides don't share a home directory.
    """
    harnesses = _harnesses_for(_home())
    win_home = _wsl_windows_home()
    if win_home is not None:
        harnesses += _harnesses_for(
            win_home, key_suffix="-win", label_suffix=" (Windows)"
        )
    return harnesses


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

    Every bundled skill goes into each detected harness's own skills dir.
    Overwrites any existing copy at each destination (explicit install) —
    a user's edits are restored too, so back them up first if that matters.

    Returns a shell-style exit code:
      0 — installed into at least one detected root (or none detected:
          nothing to do). A root that failed is reported on stderr and skipped
          in the summary, but does not change the code.
      1 — internal error (bundled skills missing from the wheel), or every
          detected root failed to write.
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
    # written once. Every bundled skill goes into each harness.
    by_root: Dict[Path, Dict[str, Path]] = {}
    for harness in present:
        dest = by_root.setdefault(harness.skills_root, {})
        for skill in bundled:
            dest[skill.name] = skill

    # `by_root` follows `_harnesses()` order, so the Linux-side roots are
    # written before any Windows-side one either way — but the guard is what
    # makes a failure on one root non-fatal for the rest.
    failed: "set[Path]" = set()
    for root, skills in by_root.items():
        try:
            _install_into(root, list(skills.values()))
        except OSError as exc:
            # A DrvFs mount (the WSL Windows-side roots) can be read-only or
            # missing the `metadata` mount option `os.chmod` needs, where the
            # Linux-side home never would be — one root's mount trouble
            # shouldn't abort installs into the others.
            print(f"Could not install into {root}: {exc}", file=sys.stderr)
            failed.add(root)

    installed_labels = [h.label for h in present if h.skills_root not in failed]
    if not installed_labels:
        print("No skills installed (see errors above).")
        return 1
    print("Installed skills into: " + ", ".join(installed_labels) + ".")
    for root in sorted(by_root, key=str):
        if root in failed:
            continue
        names = ", ".join(sorted(by_root[root]))
        print(f"  {names} → {root}")
    print(
        "  (convenience copies of the skills mnot/ietf-skill publishes; "
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
    diverged: List[Path] = []
    # The Linux-side harness table, plus every root we have previously
    # installed into (the manifest keys are `<root>/<skill>`).
    #
    # Deliberately *not* `_harnesses()`: that probes for a Windows-side home,
    # and `_wsl_windows_home()`'s cmd.exe/wslpath cold start (100ms-1s under
    # WSL) is too much to spend on every `ietf-llm <wg>` run for what is only
    # a convenience nudge. The manifest gets us the same coverage for free —
    # anything we installed Windows-side is recorded there, so it still syncs,
    # and a Windows harness we have never written to has nothing to sync. It
    # also covers roots whose harness has since left the table.
    #
    # The one thing this gives up: the manifest lives in the cache dir, which
    # a user may clear. After that the Linux-side roots are still rediscovered
    # from the table, but a Windows-side install is unmanaged — no sync, no
    # divergence notice, no orphan prune — until the next `--install-skills`
    # records it again.
    roots = {h.skills_root for h in _harnesses_for(_home())}
    roots |= {Path(dest).parent for dest in manifest}
    for root in roots:
        for name, src in bundled.items():
            dest = root / name
            if not dest.is_dir():
                continue  # not installed here — leave it alone
            if _sync_one(src, dest, manifest, verbosity, diverged):
                changed = True
    _report_diverged(diverged, verbosity)
    if _prune_orphans(manifest, set(bundled), verbosity):
        changed = True
    if changed:
        _write_manifest(manifest)


def _report_diverged(diverged: List[Path], verbosity: Verbosity) -> None:
    """One line for however many edited copies there are.

    This runs after every gather, so it has to stay small: each skill and each
    harness is named once, not once per combination.

    Phrased as a label and a list rather than a sentence, so it reads the same
    for one copy as for six — a sentence needs its verb and pronoun to agree
    with a count, and the single-copy case is the common one.
    """
    if not diverged:
        return
    home = str(_home())
    skills = sorted({d.name for d in diverged})
    places = sorted({str(d.parent.parent).replace(home, "~") for d in diverged})
    log(
        f"Edited since install, so not updated: {', '.join(skills)} "
        f"in {', '.join(places)}. "
        "Run `ietf-llm --install-skills` to overwrite local edits.",
        verbosity,
        level=LogLevel.STATUS,
    )


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
    src: Path,
    dest: Path,
    manifest: Dict[str, Any],
    verbosity: Verbosity,
    diverged: List[Path],
) -> bool:
    """Sync one installed skill against its bundled source. Mutates `manifest`
    in place; returns True if the manifest changed (so the caller persists).

    A copy the user has edited is appended to `diverged` rather than reported
    here; the caller summarises them in one line."""
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
        diverged.append(dest)
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
