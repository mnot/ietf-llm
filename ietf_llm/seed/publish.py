"""Seed-store publisher: build/refresh a static seed-store directory from the
local cache. The logic behind `scripts/publish_seeds.py`.

Producer-only. Nothing on the read-only serve path imports this module (it
reaches the gather pipeline and shells out), so it stays dormant in the package
until the operator runs the script. It never writes the cache itself — a
`--gather` run *invokes* the normal gather CLI (`python -m ietf_llm <corpus>`),
so "one writer to the cache is the gather pipeline" still holds; publishing only
*reads* the cache. See `docs/seed-store.md`.

Membership (the operator's intended set + per-corpus window) persists in
`<store>/members.json`; the consumer-facing `<store>/index.json` lists only the
corpora actually published. A run gathers each member (by default), bundles what
changed, prunes on request, and rebuilds `index.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 — used only to invoke our own gather CLI
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .. import freshness
from ..corpus import identity
from ..months import DEFAULT_MONTHS
from ..paths import get_cache_dir, get_index_dir
from . import format as fmt

#: Operator-side membership file at the store root (not consumed by clients).
MEMBERS_NAME = "members.json"

#: A gather step: run an incremental gather of `corpus` on `months`. Raises on
#: failure. Injectable so tests can stub it; the default shells out to the CLI.
GatherFn = Callable[[str, int], None]


class PublishError(Exception):
    """A publish run cannot proceed (e.g. a corpus was named that is not a
    member, or the store directory is unusable)."""


@dataclass
class MemberSpec:
    """One member's persisted publish settings."""

    window_months: int = DEFAULT_MONTHS


@dataclass
class PublishReport:
    """What a run did, for the CLI to print. Every corpus lands in exactly one
    bucket."""

    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    published: List[Tuple[str, str, int]] = field(
        default_factory=list
    )  # name, ver, bytes
    uptodate: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # name, reason


def _default_gather(corpus: str, months: int) -> None:
    """Run an incremental gather of `corpus` via the normal CLI, so the publisher
    is not a second cache writer. Raises `PublishError` on a non-zero exit."""
    cmd = [sys.executable, "-m", "ietf_llm", corpus, "--months", str(months)]
    result = subprocess.run(cmd, check=False)  # nosec B603 — fixed argv, our CLI
    if result.returncode != 0:
        raise PublishError(f"gather of {corpus!r} failed (exit {result.returncode})")


# --------------------------------------------------------------------------- #
# Membership persistence
# --------------------------------------------------------------------------- #


def _members_path(store_dir: str) -> str:
    return os.path.join(store_dir, MEMBERS_NAME)


def load_members(store_dir: str) -> Dict[str, MemberSpec]:
    """Read the store's membership, or an empty mapping for a fresh store."""
    path = _members_path(store_dir)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    out: Dict[str, MemberSpec] = {}
    for name, spec in (data.get("members") or {}).items():
        months = spec.get("window_months") if isinstance(spec, dict) else None
        out[str(name)] = MemberSpec(window_months=int(months or DEFAULT_MONTHS))
    return out


def save_members(store_dir: str, members: Dict[str, MemberSpec]) -> None:
    os.makedirs(store_dir, exist_ok=True)
    payload = {
        "format": fmt.FORMAT_VERSION,
        "members": {
            name: {"window_months": spec.window_months}
            for name, spec in sorted(members.items())
        },
    }
    tmp = _members_path(store_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, _members_path(store_dir))


def _load_index(store_dir: str) -> Optional[fmt.Index]:
    path = os.path.join(store_dir, fmt.INDEX_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return fmt.Index.from_json(handle.read())


def _write_index(store_dir: str, index: fmt.Index) -> None:
    tmp = os.path.join(store_dir, fmt.INDEX_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(index.to_json())
    os.replace(tmp, os.path.join(store_dir, fmt.INDEX_NAME))


# --------------------------------------------------------------------------- #
# Per-member publish
# --------------------------------------------------------------------------- #


def _corpus_paths(corpus: str) -> Tuple[str, str, str]:
    """`(corpus_dir, index_dir, db_path)` for `corpus` under the local cache."""
    corpus_dir = os.path.join(get_cache_dir(), corpus)
    index_dir = os.path.join(get_index_dir(), corpus)
    return corpus_dir, index_dir, os.path.join(index_dir, "embeddings.db")


def _write_member(
    store_dir: str, corpus: str, spec: MemberSpec, compat: fmt.CompatTuple, version: str
) -> fmt.IndexEntry:
    """Bundle `corpus`, write its manifest, and return the index entry. Assumes
    the corpus is gathered and compatible (the caller checked)."""
    corpus_dir, index_dir, _ = _corpus_paths(corpus)
    members = fmt.iter_bundle_members(corpus_dir, index_dir)
    bundle_rel = fmt.bundle_relpath(corpus, version)
    bundle_abs = os.path.join(store_dir, bundle_rel)
    # Drop any older bundle(s) for this corpus before writing the new one, so the
    # corpus dir holds only the current version's payload.
    _drop_old_bundles(os.path.dirname(bundle_abs), keep=os.path.basename(bundle_abs))
    digest, size = fmt.build_bundle(members, bundle_abs)
    gathered = freshness.last_gathered(corpus)
    manifest = fmt.Manifest(
        name=corpus,
        version=version,
        compat=compat,
        window_months=spec.window_months,
        gathered=freshness.iso_now() if gathered is None else gathered.isoformat(),
        bundle=bundle_rel,
        bundle_sha256=digest,
        bundle_bytes=size,
    )
    manifest_abs = os.path.join(store_dir, fmt.manifest_relpath(corpus))
    manifest_tmp = manifest_abs + ".tmp"
    with open(manifest_tmp, "w", encoding="utf-8") as handle:
        handle.write(fmt.manifest_to_json(manifest))
    os.replace(manifest_tmp, manifest_abs)
    return fmt.IndexEntry(
        name=corpus,
        kind=identity.kind_status(corpus)[0],
        subject=identity.describe(corpus),
        window_months=spec.window_months,
        gathered=manifest.gathered,
        version=version,
        manifest=fmt.manifest_relpath(corpus),
        bytes=size,
    )


def _drop_old_bundles(corpus_store_dir: str, keep: str) -> None:
    if not os.path.isdir(corpus_store_dir):
        return
    for name in os.listdir(corpus_store_dir):
        if name.endswith(".tar.gz") and name != keep:
            _rm(os.path.join(corpus_store_dir, name))


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def publish_store(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    store_dir: str,
    *,
    process: Optional[List[str]] = None,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    months: Optional[int] = None,
    no_gather: bool = False,
    force: bool = False,
    prune: bool = False,
    dry_run: bool = False,
    gather: Optional[GatherFn] = None,
) -> PublishReport:
    """Gather (unless `no_gather`), bundle, and publish members of the seed store
    at `store_dir`; rebuild `index.json`. `process` scopes the run to a subset of
    members (default: all). `add`/`remove` edit membership first. Returns a
    `PublishReport`; raises `PublishError` only on an unusable request."""
    gather = gather or _default_gather
    report = PublishReport()
    members = load_members(store_dir)

    for name in remove or []:
        if members.pop(name, None) is not None:
            report.removed.append(name)
    for name in add or []:
        members[name] = MemberSpec(window_months=months or DEFAULT_MONTHS)
        report.added.append(name)
    if (add or remove) and not dry_run:
        save_members(store_dir, members)

    if process:
        unknown = [n for n in process if n not in members]
        if unknown:
            raise PublishError(
                "not a member of this store (add it first): " + ", ".join(unknown)
            )
        targets = list(process)
    else:
        targets = sorted(members)

    prev_index = _load_index(store_dir)
    prev_entries = {e.name: e for e in prev_index.corpora} if prev_index else {}
    store_compat = prev_index.compat if prev_index else None
    # Carry forward entries for members not processed this run; drop non-members.
    entries: Dict[str, fmt.IndexEntry] = {
        n: e for n, e in prev_entries.items() if n in members
    }

    for corpus in targets:
        spec = members[corpus]
        outcome = _publish_one(
            store_dir,
            corpus,
            spec,
            prev_entries.get(corpus),
            store_compat,
            no_gather=no_gather,
            force=force,
            dry_run=dry_run,
            gather=gather,
            report=report,
        )
        if outcome is None:
            continue
        entry, store_compat = outcome
        entries[corpus] = entry

    if prune and not dry_run:
        for corpus in _published_corpus_dirs(store_dir):
            if corpus not in members:
                shutil.rmtree(os.path.join(store_dir, corpus), ignore_errors=True)
                report.pruned.append(corpus)

    if not dry_run and store_compat is not None:
        index = fmt.Index(
            generated=freshness.iso_now(),
            compat=store_compat,
            corpora=[entries[n] for n in sorted(entries)],
        )
        _write_index(store_dir, index)
    return report


def _publish_one(  # pylint: disable=too-many-arguments,too-many-return-statements
    store_dir: str,
    corpus: str,
    spec: MemberSpec,
    prev: Optional[fmt.IndexEntry],
    store_compat: Optional[fmt.CompatTuple],
    *,
    no_gather: bool,
    force: bool,
    dry_run: bool,
    gather: GatherFn,
    report: PublishReport,
) -> Optional[Tuple[fmt.IndexEntry, fmt.CompatTuple]]:
    """Publish one member. Returns `(entry, store_compat)` on publish/up-to-date,
    or None when skipped (already recorded in `report`)."""
    if not no_gather and not dry_run:
        try:
            gather(corpus, spec.window_months)
        except PublishError as err:
            report.skipped.append((corpus, str(err)))
            return None

    _, _, db_path = _corpus_paths(corpus)
    gathered = freshness.last_gathered(corpus)
    if gathered is None:
        report.skipped.append((corpus, "not gathered locally (no last-gathered)"))
        return None
    version = fmt.version_stamp(gathered)

    if prev is not None and prev.version == version and not force:
        # Unchanged since its published version — skip re-bundling. Establish the
        # store tuple from this corpus only if it is not known yet (avoids a DB
        # read on every up-to-date member once the store tuple is set).
        compat = store_compat
        if compat is None:
            try:
                compat = fmt.read_compat_tuple(db_path)
            except fmt.SeedFormatError as err:
                report.skipped.append((corpus, str(err)))
                return None
        report.uptodate.append(corpus)
        return prev, compat

    try:
        compat = fmt.read_compat_tuple(db_path)
    except fmt.SeedFormatError as err:
        report.skipped.append((corpus, str(err)))
        return None
    if store_compat is not None and compat != store_compat:
        report.skipped.append(
            (
                corpus,
                f"embedded with {compat.embedding_model} but this store is "
                f"{store_compat.embedding_model}; use a separate store dir",
            )
        )
        return None

    if dry_run:
        report.published.append((corpus, version, 0))
        return None

    entry = _write_member(store_dir, corpus, spec, compat, version)
    report.published.append((corpus, version, entry.bytes))
    return entry, compat


def _published_corpus_dirs(store_dir: str) -> List[str]:
    """Names of corpora that have a directory in the store (a manifest present)."""
    out: List[str] = []
    if not os.path.isdir(store_dir):
        return out
    for name in sorted(os.listdir(store_dir)):
        if os.path.isfile(os.path.join(store_dir, name, "manifest.json")):
            out.append(name)
    return out
