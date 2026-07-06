"""Gather WG-level metadata into a compact `group.md`.

The charter covers scope; this covers the surrounding facts a consumer
wants when orienting on a group: whether it's still active, which area
owns it, and where its repositories / home page / chat / archives live
(the Datatracker "Additional Resources"). All read from the group API
(no scraping). `digest.overview` reads this file to surface those in
its Working Group header.
"""

from __future__ import annotations

from typing import List

from ...paths import group_path
from ...utils import LogLevel, Verbosity, log
from ...atomicio import write_if_changed
from ...datatracker_api import (
    get_group_area,
    get_group_name,
    get_group_resources,
    get_group_state,
)


def write_group_info(
    wg_name: str,
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Write `group.md` for `wg_name`. Returns [path] if written, else [].

    No-op (returns []) when the API yields no state, area, or
    resources — e.g. synthetic corpora, which have no group record.
    """
    name = get_group_name(wg_name)
    state = get_group_state(wg_name)
    area = get_group_area(wg_name)
    resources = get_group_resources(wg_name)
    if not (name or state or area or resources):
        return []

    lines: List[str] = [f"# {wg_name} — working group metadata\n"]
    if name:
        lines.append(f"**Name:** {name}")
    if state:
        lines.append(f"**Status:** {state}")
    if area:
        acronym, name = area
        if acronym and name:
            lines.append(f"**Area:** {name} ({acronym})")
        else:
            lines.append(f"**Area:** {name or acronym}")
    if resources:
        lines.append("")
        lines.append("## Resources")
        for _slug, label, value in resources:
            lines.append(f"- {label}: {value}")

    content = "\n".join(lines) + "\n"
    path = group_path(cache_dir)
    if write_if_changed(path, content):
        log(f"Wrote {path}", verbose, level=LogLevel.PROGRESS)
        return [path]
    return []
