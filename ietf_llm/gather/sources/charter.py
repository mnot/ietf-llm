import os
from typing import List, Optional

from ...atomicio import atomic_open
from ...datatracker_api import get_group_type
from ...log import LogLevel, Verbosity, log
from ...net import fetch_resource
from .datatracker import _get_json


def _charter_rev(doc_name: str) -> Optional[str]:
    """Current charter revision (e.g. '09', '05-05') from the document
    API, or None if there's no charter document for the group."""
    doc = _get_json(f"/api/v1/doc/document/{doc_name}/")
    rev = doc.get("rev") if doc else None
    return rev if isinstance(rev, str) and rev else None


def process_charter(
    wg_name: str,
    output_file: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Fetch the WG charter and write to output_file. Returns list of updated files.

    The canonical charter text is a plain-text artifact published at
    www.ietf.org/charter/<doc>-<rev>.txt; the revision comes from the
    Datatracker document API. (The datatracker doc page is HTML-only,
    so we go straight to the published text rather than scraping it.)
    """
    group_type = get_group_type(wg_name)
    doc_name = f"charter-{group_type}-{wg_name}"
    rev = _charter_rev(doc_name)
    if not rev:
        # Not every group has a charter document — editorial groups
        # (e.g. RSWG), BoFs, and some non-WG/RG groups operate without
        # one. That's expected, not an error; the rest of the corpus
        # (mailing list, meetings, metadata) still gathers.
        log(
            f"No charter document for {wg_name}; skipping charter.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    url = f"https://www.ietf.org/charter/{doc_name}-{rev}.txt"
    log(f"Fetching charter for {wg_name}...", verbose, level=LogLevel.STATUS)
    res = fetch_resource(url)
    if not res:
        log(f"Error: Could not fetch charter from {url}", verbose, level=LogLevel.ERROR)
        return []

    charter_text = res.text
    if charter_text:
        # Check if the content is different from the existing file
        new_content = f"Working Group Charter: {wg_name}\n"
        new_content += f"Source: {url}\n"
        new_content += "=" * 80 + "\n\n"
        new_content += charter_text + "\n"

        if os.path.exists(output_file):
            with open(output_file, "r", encoding="utf-8") as in_fh:
                if in_fh.read() == new_content:
                    log(
                        f"Charter for {wg_name} is unchanged.",
                        verbose,
                        level=LogLevel.PROGRESS,
                    )
                    return []

        with atomic_open(output_file) as out_fh:
            out_fh.write(new_content)

        log(f"Done! Charter written to {output_file}.", verbose, level=LogLevel.STATUS)
        return [output_file]
    return []
