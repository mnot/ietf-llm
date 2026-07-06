"""Listing helpers for the `ietf-llm` CLI's `--list` / `--all` modes.

Split out of `__main__` to keep that module within its size budget; these
are self-contained cache-enumeration / table-rendering helpers with no
dependency on the gather pipeline.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import List

from .. import corpus
from ..store.corpus import get_corpus_store
from ..freshness import last_gathered
from ..utils import cached_wg_names


def discover_gathered_wgs() -> List[str]:
    """Acronyms of every WG with a files/ subdir in the cache.

    Thin alias for `utils.cached_wg_names()` — kept as a named helper
    because `--list` reads naturally with it. Local-only: `--list`'s renderer
    reads each corpus's local cache, so it has nothing to show for a corpus
    that lives only in a remote store.
    """
    return cached_wg_names()


def all_corpora() -> List[str]:
    """Every gathered corpus the configured store knows about, sorted.

    Unlike `discover_gathered_wgs`, this goes through the corpus store, so on
    the cloud backend `--all` enumerates the whole fleet from the control plane
    rather than only what this host has cached locally.
    """
    return get_corpus_store().list_corpora()


def filter_recently_used(names: List[str], days: int) -> List[str]:
    """Of `names`, those read within the last `days` days.

    Recency is the store's `last_accessed` time, falling back to `gathered_at`
    when a corpus has never been read (freshly gathered, so it gets a grace
    period rather than being dropped as a zombie). A corpus with neither
    timestamp is kept — absence of information is not evidence it's unused.
    """
    store = get_corpus_store()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: List[str] = []
    for name in names:
        when = store.last_accessed(name) or store.gathered_at(name)
        if when is None or when >= cutoff:
            out.append(name)
    return out


def print_cached_wgs() -> int:
    """Print the cached corpora — name, kind, status, last-gathered —
    to stdout. Returns 0 if any were found, 1 if the cache is empty.
    """
    wgs = discover_gathered_wgs()
    if not wgs:
        print(
            "No corpora cached yet. Run `ietf-llm <name>` "
            "(e.g. `ietf-llm httpbis`) to gather one.",
            file=sys.stderr,
        )
        return 1
    rows = []
    for wg in wgs:
        kind, status = corpus.kind_status(wg)
        when = last_gathered(wg)
        date_str = when.strftime("%Y-%m-%d") if when is not None else "unknown"
        rows.append(
            (wg, kind, corpus.status_cell(kind, status), date_str, corpus.describe(wg))
        )
    name_w = max(len(r[0]) for r in rows + [("corpus",)])
    kind_w = max(len(r[1]) for r in rows + [("", "kind")])
    status_w = max(len(r[2]) for r in rows + [("", "", "status")])
    header = (
        f"{'corpus'.ljust(name_w)}  {'kind'.ljust(kind_w)}  "
        f"{'status'.ljust(status_w)}  {'last gathered'}  about"
    )
    print(header)
    print("-" * len(header))
    for name, kind, status, date_str, subject in rows:
        line = (
            f"{name.ljust(name_w)}  {kind.ljust(kind_w)}  "
            f"{status.ljust(status_w)}  {date_str}  {subject}"
        )
        print(line.rstrip())
    return 0
