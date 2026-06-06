"""Listing helpers for the `ietf-llm` CLI's `--list` / `--all` modes.

Split out of `__main__` to keep that module within its size budget; these
are self-contained cache-enumeration / table-rendering helpers with no
dependency on the gather pipeline.
"""

from __future__ import annotations

import sys
from typing import List

from . import corpus
from .freshness import last_gathered
from .utils import cached_wg_names


def discover_gathered_wgs() -> List[str]:
    """Acronyms of every WG with a files/ subdir in the cache.

    Thin alias for `utils.cached_wg_names()` — kept as a named helper
    because `--all` and `--list` read naturally with it.
    """
    return cached_wg_names()


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
        rows.append((wg, kind, status or "—", date_str, corpus.describe(wg)))
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
