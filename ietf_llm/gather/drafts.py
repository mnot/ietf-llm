import os
import re
from typing import Any, Dict, List, Optional

from ..paths import drafts_dir
from ..utils import LogLevel, Verbosity, fetch_resource, get_group_type, log
from .datatracker import (
    draft_state_slugs,
    iter_active_drafts_by_name,
    iter_group_documents,
)
from .documents_manifest import save_documents_manifest

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
    wg_name: str,
    verbose: Verbosity = Verbosity.STATUS,
    include_related: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """List the WG's adopted drafts and published RFCs via the
    Datatracker JSON API (`/api/v1/doc/document/?group__acronym=<wg>`).

    Drafts are filtered to the `draft-<type>-<wg>-` adoption naming
    convention, so individual submissions merely associated with the
    group are skipped (matching what the old documents-page view
    surfaced). RFCs come from the group's `rfc` documents. Returns
    `{"drafts": [...], "rfcs": [...]}` in the shape
    `process_documents` consumes.

    When `include_related` is True, also merges in currently-active
    "related" drafts: individual submissions of the form
    `draft-<author>-<wg>-<topic>` (matching Datatracker's documents-page
    "Related Internet-Drafts and RFCs" section). Off by default — these
    aren't adopted, so the volume can be large for a popular WG.
    """
    log(f"Finding documents for {wg_name}...", verbose, level=LogLevel.STATUS)
    group_type = get_group_type(wg_name)
    prefix = f"draft-{group_type}-{wg_name}-"

    # One lookup of the draft-state URI→slug map (active/expired/rfc/repl/…),
    # used to classify each document below. Empty on API failure → state
    # is recorded as None and the draft is embedded (the safe default).
    state_slugs = draft_state_slugs()

    drafts: Dict[str, int] = {}
    expires: Dict[str, str] = {}
    states: Dict[str, Optional[str]] = {}
    for obj in iter_group_documents(wg_name, "draft"):
        name = obj.get("name") or ""
        if not name.startswith(prefix):
            continue
        rev = obj.get("rev")
        if not isinstance(rev, str) or not rev.isdigit():
            continue
        rev_int = int(rev)
        if name not in drafts or rev_int > drafts[name]:
            drafts[name] = rev_int
        # `expires` distinguishes a live draft (future expiry) from an
        # expired / replaced / published one (past); the overview uses
        # it to show active drafts without burying them under finished
        # work. Absent for some docs — left out of the manifest then.
        exp = obj.get("expires")
        if isinstance(exp, str) and exp:
            expires[name] = exp
        states[name] = _resolve_draft_state(obj, state_slugs)

    # Related drafts share the same {name, rev, expires} shape; merging
    # into the same dicts dedupes naturally if a name somehow appears
    # in both queries.
    if include_related and group_type:
        for obj in iter_active_drafts_by_name(wg_name):
            name = obj.get("name") or ""
            # Position-2 check: `draft-<author>-<wg>-<topic>`. Splits
            # out adoptions of other WGs whose name contains -<wg>-
            # (e.g. draft-ietf-mailmaint-oauth-public) and ill-formed
            # names where <wg> isn't the second slug.
            parts = name.split("-")
            if len(parts) < 4 or parts[2] != wg_name:
                continue
            rev = obj.get("rev")
            if not isinstance(rev, str) or not rev.isdigit():
                continue
            rev_int = int(rev)
            if name not in drafts or rev_int > drafts[name]:
                drafts[name] = rev_int
            exp = obj.get("expires")
            if isinstance(exp, str) and exp:
                expires[name] = exp
            states[name] = _resolve_draft_state(obj, state_slugs)

    rfcs: Dict[str, str] = {}
    for obj in iter_group_documents(wg_name, "rfc"):
        name = obj.get("name") or ""
        match = re.match(r"rfc(\d+)$", name)
        if match:
            rfcs[name] = match.group(1)

    return {
        "drafts": [
            {
                "name": n,
                "max_rev": r,
                "expires": expires.get(n, ""),
                "state": states.get(n),
            }
            for n, r in drafts.items()
        ],
        "rfcs": [{"name": n, "number": num} for n, num in rfcs.items()],
    }


def _resolve_draft_state(
    obj: Dict[str, Any], state_slugs: Dict[str, str]
) -> Optional[str]:
    """Return a draft document's draft-type state slug, or None.

    A document carries several state URIs (draft, IESG, stream, …); only
    one belongs to the `draft` state type, so we return the first of
    `obj["states"]` that appears in `state_slugs` (the draft-type map).
    None when the map is empty (API failure) or no draft-type state is
    present.
    """
    for uri in obj.get("states") or []:
        slug = state_slugs.get(uri)
        if slug is not None:
            return slug
    return None


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
    include_related: bool = False,
) -> List[str]:
    """Download all revisions of WG drafts and RFCs as text.

    Drafts and RFCs live under `drafts/` in the WG cache. The
    `destination` argument is the WG's `files/` dir; we materialise
    the `drafts/` subdir as needed.

    When `include_related` is True, also pulls active individual
    `draft-<author>-<wg>-<topic>` drafts (see `get_wg_documents`).
    """
    updated = []
    docs = get_wg_documents(wg_name, verbose, include_related=include_related)
    out_dir = drafts_dir(destination)
    os.makedirs(out_dir, exist_ok=True)

    # 1. Process Drafts
    drafts = docs["drafts"]
    if drafts:
        # Record every draft (not only ones with an expiry): the manifest
        # now also drives which drafts the embedding index skips, keyed on
        # Datatracker state, and a concluded draft may carry no expiry.
        save_documents_manifest(
            wg_name,
            {
                str(d["name"]): {
                    "expires": str(d.get("expires") or ""),
                    "state": d.get("state"),
                }
                for d in drafts
            },
        )
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
