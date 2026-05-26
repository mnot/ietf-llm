import os
import re
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from .utils import LogLevel, Verbosity, log, fetch_resource, get_group_type


def get_wg_documents(
    wg_name: str, verbose: Verbosity = Verbosity.STATUS
) -> Dict[str, List[Dict[str, Any]]]:
    """Scrape WG documents page for drafts and RFCs."""
    url = f"https://datatracker.ietf.org/group/{wg_name}/documents/"
    log(f"Finding documents for {wg_name}...", verbose, level=LogLevel.STATUS)
    res = fetch_resource(url)
    if not res:
        return {"drafts": [], "rfcs": []}

    soup = BeautifulSoup(res.text, "html.parser")
    drafts: List[Dict[str, Any]] = []
    rfcs: List[Dict[str, Any]] = []

    # Patterns
    group_type = get_group_type(wg_name)
    prefix = f"draft-{group_type}-{wg_name}-"
    draft_pattern = f"/doc/{prefix}"

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href")
        if not isinstance(href, str):
            continue

        # Check for RFCs
        if "/doc/rfc" in href:
            rfc_match = re.search(r"/doc/rfc(\d+)/", href)
            if rfc_match:
                rfc_num = rfc_match.group(1).lstrip("0")
                rfcs.append({"name": f"rfc{rfc_num}", "number": rfc_num})
                continue

        # Check for Drafts
        if draft_pattern in href:
            text = a_tag.get_text(strip=True)
            # Text usually looks like "draft-ietf-wg-name-something-05"
            match = re.search(r"(" + re.escape(prefix) + r".*?)-(\d+)$", text)
            if match:
                draft_name = match.group(1)
                try:
                    current_rev = int(match.group(2))
                    drafts.append({"name": draft_name, "max_rev": current_rev})
                except ValueError:
                    continue

    # De-duplicate drafts and keep the highest revision found
    unique_drafts: Dict[str, int] = {}
    for draft_entry in drafts:
        d_name = str(draft_entry["name"])
        d_rev = int(draft_entry["max_rev"])
        if d_name not in unique_drafts or d_rev > unique_drafts[d_name]:
            unique_drafts[d_name] = d_rev

    # De-duplicate RFCs
    unique_rfcs: Dict[str, str] = {}
    for rfc_entry in rfcs:
        r_name = str(rfc_entry["name"])
        r_num = str(rfc_entry["number"])
        unique_rfcs[r_name] = r_num

    return {
        "drafts": [
            {"name": name, "max_rev": rev} for name, rev in unique_drafts.items()
        ],
        "rfcs": [{"name": name, "number": num} for name, num in unique_rfcs.items()],
    }


def process_documents(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Download all revisions of WG drafts and RFCs as text."""
    updated = []
    docs = get_wg_documents(wg_name, verbose)

    # 1. Process Drafts
    drafts = docs["drafts"]
    if drafts:
        for draft in drafts:
            name = str(draft["name"])
            max_rev = int(draft["max_rev"])
            log(
                f"Processing draft: {name} (revs 00 to {max_rev:02d})",
                verbose,
                level=LogLevel.STATUS,
            )

            for rev in range(max_rev + 1):
                rev_str = f"{rev:02d}"
                filename = f"{name}-{rev_str}.txt"
                filepath = os.path.join(destination, filename)

                if os.path.exists(filepath):
                    continue

                url = f"https://www.ietf.org/archive/id/{name}-{rev_str}.txt"
                log(f"Downloading {filename}...", verbose, level=LogLevel.PROGRESS)
                res = fetch_resource(url)
                if res:
                    with open(filepath, "w", encoding="utf-8") as out_fh:
                        out_fh.write(str(res.text))
                    updated.append(filepath)
    else:
        log(f"No drafts found for {wg_name}.", verbose, level=LogLevel.STATUS)

    # 2. Process RFCs
    rfcs = docs["rfcs"]
    if rfcs:
        for rfc in rfcs:
            r_name = str(rfc["name"])
            r_num = str(rfc["number"])
            filename = f"{r_name}.txt"
            filepath = os.path.join(destination, filename)

            if os.path.exists(filepath):
                continue

            url = f"https://www.rfc-editor.org/rfc/rfc{r_num}.txt"
            log(f"Downloading {filename}...", verbose, level=LogLevel.PROGRESS)
            res = fetch_resource(url)
            if res:
                with open(filepath, "w", encoding="utf-8") as out_fh:
                    out_fh.write(str(res.text))
                updated.append(filepath)
    else:
        log(f"No RFCs found for {wg_name}.", verbose, level=LogLevel.STATUS)

    return updated
