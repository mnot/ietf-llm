"""Install and refresh the RFC full-text corpus from the seed store.

The ordinary seed path is keyed to the corpus being gathered: `ietf-llm
httpbis` seeds `httpbis`. That never reaches this corpus, because there is
no gather for it — nobody types `ietf-llm rfcs`, and if they did there would
be nothing to run. So it gets the trigger the RFC *metadata* mirror already
uses: once per invocation, from tail housekeeping, whatever corpus the user
was actually working on (issue #230).

That placement is deliberate. This is a foundational capability rather than
one effort's record — anything you ask about a working group is likely to
touch the RFCs it produced — and the project already fetches ~130 MB of seed
bundle on a first gather without being asked, so an automatic pull here is a
difference of degree. It is opt-out on the same switch as everything else:
`--no-seed` / `IETF_LLM_SEED_ENABLED=off` covers it.

**Refresh keys on the upstream build, not on time gathered.** This corpus has
no gather time; what distinguishes one snapshot from another is which
upstream artifact it was assembled from, which both the local index and the
store entry record. The staleness margin is shared with the ordinary seed
path, for a different reason: there it stops a client re-downloading a delta
it would re-embed for free, here there is no incremental path at all and the
margin is purely a bandwidth guard — ~270 MiB for a month's worth of new
RFCs is not worth it, two months' worth is. The local copy therefore runs a
month or two behind, which every RFC tool discloses in its output.

Best-effort throughout: any failure logs and leaves what is already
installed, exactly like the metadata mirror beside it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from ...config import service as service_config
from ...log import LogLevel, Verbosity, log
from ...paths import get_index_dir

#: The corpus name the RFC series installs under. Defined here rather than
#: imported from `mcp` so the gather path does not pull the MCP surface in.
RFC_CORPUS = "rfcs"

#: `meta` key recording which upstream build the local corpus came from.
_BUILD_KEY = "rfc_index_build"

#: Build ids are `YYYYMMDDTHHMMSSZ` — a legal git ref, and parseable.
_BUILD_FORMAT = "%Y%m%dT%H%M%SZ"

#: Don't ask the store for its index more often than this. Upstream
#: republishes about monthly and the staleness margin means a re-seed lands
#: about every second month, so checking once an hour is already far more
#: often than anything can change — and this runs on *every* `ietf-llm`
#: invocation, where an unthrottled fetch would be a request per run for a
#: document that moves six times a year. Mirrors the throttle
#: `ensure_rfc_index` uses beside it.
_CHECK_INTERVAL = 3600.0

#: Touched after each check; its mtime is the throttle.
_STAMP = "last-checked"


def _db_path() -> str:
    return os.path.join(get_index_dir(), RFC_CORPUS, "embeddings.db")


def _stamp_path() -> str:
    return os.path.join(get_index_dir(), RFC_CORPUS, _STAMP)


def _checked_recently(interval: float) -> bool:
    try:
        return (time.time() - os.path.getmtime(_stamp_path())) < interval
    except OSError:
        return False


def _touch_stamp() -> None:
    path = _stamp_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except OSError:
        pass  # best-effort: a missing stamp only costs an extra check


def local_build() -> Optional[str]:
    """The upstream build id of the installed corpus, or None if absent."""
    path = _db_path()
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (_BUILD_KEY,)
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _parse_build(build: str) -> Optional[datetime]:
    try:
        return datetime.strptime(build, _BUILD_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _should_install(have: Optional[str], offered: str, margin_days: float) -> bool:
    """Whether `offered` is worth the download over `have`.

    An unparseable build id on either side falls through to installing: the
    ids are ours to produce, so a shape we cannot read means something has
    changed and the fresher artifact is the safer answer.
    """
    if have is None:
        return True  # cold
    if have == offered:
        return False
    mine, theirs = _parse_build(have), _parse_build(offered)
    if mine is None or theirs is None:
        return True
    if theirs <= mine:
        return False  # the store is behind us; keep what we have
    return (theirs - mine).total_seconds() > margin_days * 86400.0


def ensure_rfc_corpus(  # pylint: disable=too-many-return-statements
    verbosity: Verbosity = Verbosity.STATUS,
    margin_days: Optional[float] = None,
    interval: float = _CHECK_INTERVAL,
) -> None:
    """Install or refresh the RFC full-text corpus, best-effort."""
    if not service_config.seeding_enabled():
        return
    seed_url = service_config.seed_url()
    if not seed_url:
        return
    if _checked_recently(interval):
        return
    # Lazy: the seed consumer is gather-path only, and this keeps the import
    # off any path that merely reads.
    # pylint: disable=import-outside-toplevel
    from ...seed import fetch as seed_fetch
    from ...seed import format as seed_fmt
    from ..sequencer import _seed_stale_jump_margin

    # pylint: enable=import-outside-toplevel

    # Stamped on the attempt, not on success: a store that is unreachable or
    # carries no index would otherwise be retried on every single invocation,
    # which is the case the throttle most needs to cover.
    _touch_stamp()
    try:
        index = seed_fetch.load_index(seed_url)
    except Exception:  # pylint: disable=broad-except
        return
    if index is None:
        return
    entry = index.entry(RFC_CORPUS)
    if entry is None:
        return  # the store does not carry it — stay quiet

    if margin_days is None:
        margin_days = _seed_stale_jump_margin().total_seconds() / 86400.0
    have = local_build()
    if not _should_install(have, entry.version, margin_days):
        return

    try:
        seed_fetch.install(seed_url, entry)
    except (seed_fetch.SeedFetchError, seed_fmt.SeedFormatError) as err:
        log(
            f"{RFC_CORPUS}: seed failed ({err}); RFC full-text search will use "
            "whatever is already installed.",
            verbosity,
            level=LogLevel.STATUS,
        )
        return
    what = "installed" if have is None else f"updated from build {have}"
    log(
        f"{RFC_CORPUS}: RFC full-text corpus {what} (build {entry.version})",
        verbosity,
        level=LogLevel.STATUS,
    )
