import os
from typing import Any, Dict, List, Optional, Tuple

from ...atomicio import atomic_open
from ...datatracker_api import get_group_type
from ...log import LogLevel, Verbosity, log
from ...net import fetch_resource
from .datatracker import _get_json

#: Datatracker charter state `approved`; its URI ends in this id.
_CHARTER_APPROVED = "88"


def _uri_id(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def approved_rev(charter: Dict[str, Any]) -> Tuple[str, str]:
    """`(revision of the approved charter, status)` for a charter document.

    A plain revision (`09`) is an approved charter. A dashed one (`01-05`) is
    a working revision: with a non-zero base, a recharter is in progress and
    the approved text is the base revision (`01`); with base `00` the charter
    was never approved. The status is "" when the document's latest revision
    is the approved one, else `rechartering` or `unapproved`.
    """
    rev = str(charter.get("rev") or "")
    approved_state = any(
        _uri_id(str(s)) == _CHARTER_APPROVED for s in charter.get("states") or []
    )
    if not rev or "-" not in rev or approved_state:
        return rev, ""
    base = rev.split("-", 1)[0]
    if base.strip("0"):
        return base, "rechartering"
    return rev, "unapproved"


def _charter_doc(doc_name: str) -> Optional[Dict[str, Any]]:
    """The charter document record, or None if the group has no charter
    document (or the lookup failed)."""
    doc = _get_json(f"/api/v1/doc/document/{doc_name}/")
    rev = doc.get("rev") if doc else None
    return doc if doc and isinstance(rev, str) and rev else None


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

    While a group is rechartering, the document's current revision is a
    dashed working draft (`03-02`); the approved charter is the base
    revision (`03`), so that is what is written, with a header line naming
    the proposal.
    """
    group_type = get_group_type(wg_name)
    doc_name = f"charter-{group_type}-{wg_name}"
    doc = _charter_doc(doc_name)
    if not doc:
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

    rev, status = approved_rev(doc)
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
        if status == "rechartering":
            working = doc["rev"]
            new_content += (
                f"Rechartering in progress: proposed revision {working} at "
                f"https://www.ietf.org/charter/{doc_name}-{working}.txt\n"
            )
        elif status == "unapproved":
            new_content += "Note: this charter was never approved.\n"
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
