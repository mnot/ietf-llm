"""Build the RFC-series corpus from upstream, end to end.

One call chains what the rest of this package does piecewise: find the
newest index rfc.fyi has released, fetch it, bring the text mirror level
with it, drop any RFC whose bytes have moved since that build, and assemble
an `embeddings.db`.

This is the seam the seed publisher uses. It exists so that adding the RFC
member to a store is a *member kind*, not a pile of new operator steps: the
inputs are two public sources and the output is an ordinary index, so
nothing machine-specific has to be configured for it.

**The mirror defaults under the cache**, beside the metadata singleton it
belongs with, rather than becoming another path an operator has to name.
It is ~530 MB and persistent — rsync keeps it level in seconds after the
first run — and it is the publisher's alone: a client installs the finished
corpus and never fetches RFC text at all.

Publisher-side only — see the subpackage docstring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..log import LogLevel, Verbosity, log
from ..paths import get_cache_dir
from .build import BuildStats, assembly_version, build_rfc_index
from .fetch import IndexRelease, download_index, latest_release
from .format import RfcIndexError, read_sources
from .mirror import Reconciliation, reconcile, sync_mirror

#: Where the publisher keeps RFC plain text. Under the cache, beside the
#: `_rfc/` metadata singleton, because it is the same subject and neither is
#: a corpus. Override with `IETF_LLM_RFC_MIRROR` if the 530 MB wants to live
#: on another volume.
_MIRROR_ENV = "IETF_LLM_RFC_MIRROR"
_MIRROR_SUBDIR = os.path.join("_rfc", "text")

#: Where the fetched upstream index is unpacked. Kept rather than discarded
#: so a re-run with an unchanged release skips the download.
_STAGE_SUBDIR = os.path.join("_rfc", "upstream")


def mirror_dir() -> str:
    """The RFC plain-text mirror path."""
    override = os.environ.get(_MIRROR_ENV, "").strip()
    return override or os.path.join(get_cache_dir(), _MIRROR_SUBDIR)


def stage_dir() -> str:
    return os.path.join(get_cache_dir(), _STAGE_SUBDIR)


@dataclass
class PublishResult:
    """What a build produced, for the caller to log and to decide on."""

    release: IndexRelease
    db_path: str
    stats: BuildStats
    reconciliation: Reconciliation
    #: False when the newest release was already built into `db_path`.
    rebuilt: bool = True


def _existing_build(db_path: str) -> Optional[str]:
    """The upstream build id already assembled at `db_path`, if any."""
    # pylint: disable-next=import-outside-toplevel
    import sqlite3

    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='rfc_index_build'"
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def build_from_upstream(
    db_path: str,
    force: bool = False,
    verbosity: Verbosity = Verbosity.STATUS,
) -> Optional[PublishResult]:
    """Assemble the RFC corpus at `db_path` from the newest published index.

    Returns None when rfc.fyi has no index release at all — which is a real
    state, not a failure: their own deploy treats a missing index as "publish
    without full-text search", and a publisher with nothing new to bundle
    should say so rather than raise.

    Skips the work when `db_path` was already built from that same release,
    unless `force`.
    """
    release = latest_release()
    if release is None:
        log("no published RFC index release upstream", verbosity, LogLevel.WARN)
        return None

    have = _existing_build(db_path)
    if have == assembly_version(release.build) and not force:
        # Returned without consulting the staged index at all: the corpus is
        # already the artifact this release describes, so a missing stage dir
        # is no reason to re-download ~130 MB to confirm it.
        log(f"RFC corpus already built from {release.tag}", verbosity, LogLevel.STATUS)
        return PublishResult(
            release=release,
            db_path=db_path,
            stats=BuildStats(),
            reconciliation=Reconciliation(),
            rebuilt=False,
        )

    index_dir = download_index(release, stage_dir(), verbosity)
    sync_mirror(mirror_dir(), verbosity=verbosity)

    digests = read_sources(index_dir)
    result = reconcile(mirror_dir(), digests)
    log(f"RFC text mirror: {result.summary()}", verbosity, LogLevel.STATUS)
    if digests and not result.matched:
        # Every RFC differs or is missing: the mirror is not the one this
        # build saw at all, and a corpus assembled from it would be empty or
        # wrong throughout. Better to fail the publish than ship that.
        raise RfcIndexError(
            f"no RFC in the mirror matches {release.tag}; "
            "the mirror and the upstream build have diverged completely"
        )

    stats = build_rfc_index(index_dir, mirror_dir(), db_path, verbosity=verbosity)
    return PublishResult(
        release=release, db_path=db_path, stats=stats, reconciliation=result
    )
