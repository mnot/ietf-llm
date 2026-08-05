"""Which mailing lists is a `--author` person actually on?

The archive has no cross-list by-sender search — IMAP `SEARCH FROM` is
per-folder — so following a person's mail means naming the folders
first. This module proposes the candidates; `mbox.sync_author_mail`
does the searching.

Candidates come from three places, all of which say "this person has
standing here" without anyone having to say so:

  - **Groups they hold a role in** (`group/role/?person=`) — chair,
    secretary, AD, directorate reviewer. The strongest signal, and the
    one that catches lists they read and post to without ever writing a
    draft.
  - **Groups that own a draft they authored.** Weaker (an author may
    never post) but it covers WGs they worked in without a formal role.
  - **`last-call@` and `ietf@`**, always. Cross-area review happens
    there and nowhere else, it is where a reviewer's breadth actually
    shows, and no per-person signal would ever surface them. `ietf@`
    carries the pre-2020 Last Call traffic, so a career-spanning corpus
    needs both.

A group's list is its Datatracker `list_email`, resolved through
`get_mailing_list_name` so that a WG hosted off IETF infrastructure
(httpbis runs at w3.org) resolves to the IETF mirror the IMAP server
actually exposes. Groups with no `list_email` — areas, IAB ASGs — drop
out there.

**Candidates, not conclusions.** A list where the person never posted
costs one IMAP SEARCH that returns nothing, so there is no ranking pass
and no activity threshold here: the sender-scoped search downstream *is*
the ranking, and it is cheaper than any proxy for it would be.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

from ...datatracker_api import get_mailing_list_name
from ...log import LogLevel, Verbosity, log
from .datatracker import _get_json  # pylint: disable=protected-access
from .mbox import normalize_list_name, validate_list_names

_API_BASE = "https://datatracker.ietf.org/api/v1"

#: Always followed for a person corpus, regardless of their groups.
#: `last-call` is where IETF Last Call review happens today; `ietf` is
#: where it happened before the 2020 split.
ALWAYS_LISTS = ("last-call", "ietf")

#: Ceiling on discovered lists (excluding ALWAYS_LISTS). A long career
#: can accumulate 20+ groups; each one costs an IMAP connect and search
#: even when it yields nothing. Anything dropped is logged — a silent
#: truncation would read as "these are all their lists".
MAX_DISCOVERED = 25

_GROUP_ID = re.compile(r"/group/group/(\d+)/?$")


def _group_ids_from_roles(person_id: int) -> List[str]:
    """Group ids where the person holds any role, in API order."""
    body = _get_json(f"{_API_BASE}/group/role/?person={person_id}&limit=200")
    out: List[str] = []
    for obj in (body or {}).get("objects") or []:
        match = _GROUP_ID.search(str(obj.get("group") or ""))
        if match:
            out.append(match.group(1))
    return out


def _group_ids_from_drafts(draft_names: Iterable[str]) -> List[str]:
    """Group ids owning the named drafts.

    Batched through `name__in` rather than parsed out of the draft name:
    `draft-ietf-<acronym>-…` is only a convention, and an individual
    submission (`draft-nottingham-…`) has no acronym in it at all.
    """
    names = [n for n in draft_names if n]
    out: List[str] = []
    for start in range(0, len(names), 100):
        batch = ",".join(names[start : start + 100])
        body = _get_json(f"{_API_BASE}/doc/document/?name__in={batch}&limit=100")
        for obj in (body or {}).get("objects") or []:
            match = _GROUP_ID.search(str(obj.get("group") or ""))
            if match:
                out.append(match.group(1))
    return out


def _fetch_groups(group_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Batch-dereference groups by id."""
    ordered = sorted(set(group_ids))
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ordered), 100):
        batch = ",".join(ordered[start : start + 100])
        body = _get_json(f"{_API_BASE}/group/group/?id__in={batch}&limit=100")
        for obj in (body or {}).get("objects") or []:
            if obj.get("id") is not None:
                out[str(obj["id"])] = obj
    return out


def _acronyms_in_order(
    ordered_ids: List[str], groups: Dict[str, Dict[str, Any]]
) -> List[str]:
    """Acronyms of the groups that have a mailing list, first-seen order.

    An empty `list_email` is the filter that drops areas, IAB ASGs, and
    other groups that exist on Datatracker but have no list of their
    own.
    """
    seen: Set[str] = set()
    out: List[str] = []
    for gid in ordered_ids:
        group = groups.get(gid)
        if not group or not (group.get("list_email") or "").strip():
            continue
        acronym = str(group.get("acronym") or "").strip()
        if acronym and acronym not in seen:
            seen.add(acronym)
            out.append(acronym)
    return out


def discover_author_lists(
    person_id: int,
    draft_names: Optional[Iterable[str]] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Mailing lists worth searching for `person_id`'s messages.

    Returns normalised archive names (`httpbisa`, `last-call`, …),
    validated against mailarchive so a group whose list isn't archived
    there doesn't cost an IMAP round-trip to discover the same thing.
    `ALWAYS_LISTS` come first and are never dropped by the cap.
    """
    role_ids = _group_ids_from_roles(person_id)
    draft_ids = _group_ids_from_drafts(draft_names or [])
    # Roles first: a role is the stronger claim, and first-seen order is
    # what the cap truncates against.
    ordered_ids = role_ids + draft_ids
    acronyms = _acronyms_in_order(ordered_ids, _fetch_groups(ordered_ids))

    discovered: List[str] = []
    seen: Set[str] = {normalize_list_name(name) for name in ALWAYS_LISTS}
    for acronym in acronyms:
        name = normalize_list_name(get_mailing_list_name(acronym))
        if name and name not in seen:
            seen.add(name)
            discovered.append(name)

    if len(discovered) > MAX_DISCOVERED:
        log(
            f"  {len(discovered)} candidate list(s) from groups; keeping the "
            f"first {MAX_DISCOVERED} (roles before draft-only groups). "
            f"Dropped: {', '.join(discovered[MAX_DISCOVERED:])}.",
            verbose,
            level=LogLevel.WARN,
        )
        discovered = discovered[:MAX_DISCOVERED]

    candidates = list(ALWAYS_LISTS) + discovered
    # A group's `list_email` can name a list mailarchive doesn't carry
    # (a review team inheriting its WG's off-IETF address, say), so
    # validate before spending an IMAP connect on it. Labelled `--author`
    # because the user never typed these names.
    valid = validate_list_names(candidates, verbose, source="--author")
    log(
        f"  {len(valid)} mailing list(s) to search for this person: "
        f"{', '.join(valid)}.",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return valid
