import os
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from ..paths import drafts_dir
from ..utils import LogLevel, Verbosity, fetch_resource, get_group_type, log

# `draft-foo-bar-07.txt` / `draft-foo-bar-07` / `draft-foo-bar.txt` /
# `draft-foo-bar` all normalise to `draft-foo-bar`. Used by both
# `--draft` argument parsing and by `process_extra_drafts` so callers
# can pass whatever form they have without thinking.
_DRAFT_VERSION_SUFFIX_RE = re.compile(r"-\d{2}(?:\.txt)?$")
_DRAFT_TXT_SUFFIX_RE = re.compile(r"\.txt$")


def normalize_draft_name(name: str) -> str:
    """Return the version-less base draft name.

    `draft-foo-bar-07.txt` → `draft-foo-bar`
    `draft-foo-bar-07`     → `draft-foo-bar`
    `draft-foo-bar.txt`    → `draft-foo-bar`
    `draft-foo-bar`        → `draft-foo-bar`
    """
    cleaned = name.strip()
    cleaned = _DRAFT_VERSION_SUFFIX_RE.sub("", cleaned)
    cleaned = _DRAFT_TXT_SUFFIX_RE.sub("", cleaned)
    return cleaned


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


def fetch_current_rev(
    draft_name: str, verbose: Verbosity = Verbosity.STATUS
) -> Optional[int]:
    """Resolve a draft's current revision via the Datatracker JSON API.

    Returns the integer revision (e.g. 7 for `-07`) or None on
    failure. Used for `--draft` additions where we don't know what
    revisions exist without asking.
    """
    url = (
        f"https://datatracker.ietf.org/api/v1/doc/document/"
        f"{draft_name}/?format=json"
    )
    res = fetch_resource(url)
    if not res:
        return None
    try:
        body = res.json()
    except ValueError:
        return None
    rev = body.get("rev") if isinstance(body, dict) else None
    if not isinstance(rev, str) or not rev.isdigit():
        return None
    try:
        return int(rev)
    except ValueError:
        return None


def validate_draft_names(
    names: List[str], verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Return the subset of `names` that resolve on Datatracker.

    Used by the CLI to drop typo'd `--draft` arguments BEFORE
    `config.merge` writes them to disk: a bad name shouldn't end up
    sticky in `gather.json`, where it would re-fail every subsequent
    run. Names are normalised (version suffix stripped) before
    lookup; the returned list preserves the user's original casing /
    form so the persisted value matches what they typed.
    """
    valid: List[str] = []
    for raw in names:
        normalised = normalize_draft_name(raw)
        if not normalised.startswith("draft-"):
            log(
                f"--draft {raw!r}: doesn't look like a draft name; " "not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        rev = fetch_current_rev(normalised, verbose)
        if rev is None:
            log(
                f"--draft {raw}: Datatracker doesn't know this "
                "draft; not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        valid.append(raw)
    return valid


def _download_all_revisions(
    draft_name: str,
    max_rev: int,
    out_dir: str,
    verbose: Verbosity,
) -> List[str]:
    """Pull every revision (00..max_rev) of one draft into out_dir.
    Returns the paths of newly-written files (skips revisions whose
    .txt is already cached)."""
    updated: List[str] = []
    log(
        f"Processing draft: {draft_name} (revs 00 to {max_rev:02d})",
        verbose,
        level=LogLevel.STATUS,
    )
    for rev in range(max_rev + 1):
        rev_str = f"{rev:02d}"
        filename = f"{draft_name}-{rev_str}.txt"
        filepath = os.path.join(out_dir, filename)
        if os.path.exists(filepath):
            continue
        url = f"https://www.ietf.org/archive/id/{draft_name}-{rev_str}.txt"
        log(f"Downloading {filename}...", verbose, level=LogLevel.PROGRESS)
        res = fetch_resource(url)
        if res:
            with open(filepath, "w", encoding="utf-8") as out_fh:
                out_fh.write(str(res.text))
            updated.append(filepath)
    return updated


def process_extra_drafts(
    draft_names: List[str],
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Download every revision of each given draft.

    Use for drafts that aren't auto-discovered as WG documents on
    Datatracker — typically `--draft draft-<author>-<wg>-<topic>`
    additions where the WG follows but doesn't own the draft (or
    where the author hasn't yet asked for adoption). Each name is
    version-stripped first, so `draft-foo-bar`, `draft-foo-bar-07`,
    and `draft-foo-bar-07.txt` all yield the same result.

    Resolves the current revision via Datatracker so we know how
    many to fetch. Skips silently for drafts the API can't find —
    a typoed name shouldn't kill the whole gather.
    """
    if not draft_names:
        return []
    updated: List[str] = []
    out_dir = drafts_dir(destination)
    os.makedirs(out_dir, exist_ok=True)
    for raw in draft_names:
        name = normalize_draft_name(raw)
        if not name.startswith("draft-"):
            log(
                f"--draft {raw!r}: doesn't look like a draft name; skipping.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        max_rev = fetch_current_rev(name, verbose)
        if max_rev is None:
            log(
                f"--draft {name}: Datatracker doesn't know this draft; " "skipping.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        updated.extend(_download_all_revisions(name, max_rev, out_dir, verbose))
    return updated


def process_documents(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Download all revisions of WG drafts and RFCs as text.

    Drafts and RFCs live under `drafts/` in the WG cache. The
    `destination` argument is the WG's `files/` dir; we materialise
    the `drafts/` subdir as needed.
    """
    updated = []
    docs = get_wg_documents(wg_name, verbose)
    out_dir = drafts_dir(destination)
    os.makedirs(out_dir, exist_ok=True)

    # 1. Process Drafts
    drafts = docs["drafts"]
    if drafts:
        for draft in drafts:
            name = str(draft["name"])
            max_rev = int(draft["max_rev"])
            updated.extend(_download_all_revisions(name, max_rev, out_dir, verbose))
    else:
        log(f"No drafts found for {wg_name}.", verbose, level=LogLevel.STATUS)

    # 2. Process RFCs
    rfcs = docs["rfcs"]
    if rfcs:
        for rfc in rfcs:
            r_name = str(rfc["name"])
            r_num = str(rfc["number"])
            filename = f"{r_name}.txt"
            filepath = os.path.join(out_dir, filename)

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
