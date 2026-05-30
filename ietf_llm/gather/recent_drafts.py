"""Discover recently-submitted brand-new Internet-Drafts.

For a "keep up with new I-Ds" corpus: query the submission API for
`-00` revisions (the first revision is what makes a draft *new*)
submitted within the recency window, and return their names so the
draft pipeline can fetch them.

The tastypie document endpoint can't order/filter by time, but the
submission endpoint filters on `rev` and `submission_date`, which is
exactly the new-draft signal:

    /api/v1/submit/submission/?rev=00&submission_date__gte=<cutoff>

`-00` still includes WG adoptions / renames (a `-00` that `replaces`
an earlier individual draft); we keep those — they're new documents in
their own right.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from ..paths import drafts_dir
from ..utils import LogLevel, Verbosity, log
from .datatracker import _get_json
from .drafts import normalize_draft_name

_SUBMIT_API = "https://datatracker.ietf.org/api/v1/submit/submission/"


def fetch_new_draft_names(
    months: int, verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Names of drafts whose `-00` was submitted in the last `months`.

    Paginated by descending id (the submission endpoint only orders by
    id). De-duplicated, preserving newest-first order.
    """
    cutoff = (datetime.now() - timedelta(days=30 * months)).strftime("%Y-%m-%d")
    log(
        f"Finding new (-00) Internet-Drafts submitted since {cutoff}...",
        verbose,
        level=LogLevel.STATUS,
    )
    # `state=posted` excludes cancelled / pending submissions — those
    # aren't published documents and would 404 (noisily) when fetched.
    path: Optional[str] = (
        f"{_SUBMIT_API}?rev=00&state=posted&submission_date__gte={cutoff}"
        "&order_by=-id&limit=200"
    )
    seen: set[str] = set()
    names: List[str] = []
    while path:
        body = _get_json(path)
        if not body:
            break
        for obj in body.get("objects") or []:
            name = obj.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(str(name))
        path = (body.get("meta") or {}).get("next") or None
    log(
        f"  {len(names)} new draft(s) in the window.",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return names


def prune_drafts(
    cache_dir: str, keep_names: Iterable[str], verbose: Verbosity = Verbosity.STATUS
) -> int:
    """Delete cached draft `.txt` files whose draft is not in `keep_names`.

    The rolling window: a new-drafts subscription reflects exactly the
    current window, so a draft that has aged out is removed. `keep_names`
    must include any explicit `--draft` additions so those are never
    pruned. RFC files (`rfc*.txt`) are left alone. Returns the count of
    removed files.
    """
    keep = {normalize_draft_name(n) for n in keep_names}
    directory = drafts_dir(cache_dir)
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for fname in os.listdir(directory):
        if not fname.startswith("draft-") or not fname.endswith(".txt"):
            continue
        if normalize_draft_name(fname) not in keep:
            try:
                os.remove(os.path.join(directory, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        log(
            f"  pruned {removed} draft file(s) outside the window.",
            verbose,
            level=LogLevel.PROGRESS,
        )
    return removed
