"""Local mirror of the seed store's index (issue #182), so read tools can show
what is available to fast-start without a network fetch.

Written on the gather path (when `_maybe_seed` fetches the index) and read
**offline** by `list_corpora`. Read-safe by design: it imports only stdlib,
`seed.format`, and `paths` — never the network-touching `seed.fetch` — so a read
tool can import it without crossing the offline boundary. The cache is a plain
local file (`paths.seed_index_cache_path`), refreshed every time a gather fetches
the index; a read only ever sees the last-fetched snapshot, never the network.
"""

from __future__ import annotations

import os
from typing import Optional

from ..paths import seed_index_cache_path
from . import format as fmt


def cache_index(index: fmt.Index) -> None:
    """Persist `index` to the local mirror (best-effort — a failure is ignored;
    the catalog hint is a convenience, not a correctness invariant)."""
    path = seed_index_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(index.to_json())
        os.replace(tmp, path)
    except OSError:
        pass


def cached_index() -> Optional[fmt.Index]:
    """The last-mirrored seed index, or None if never fetched / unreadable."""
    try:
        with open(seed_index_cache_path(), "r", encoding="utf-8") as handle:
            return fmt.Index.from_json(handle.read())
    except (OSError, fmt.SeedFormatError):
        return None
