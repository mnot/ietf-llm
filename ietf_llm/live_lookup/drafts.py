"""Live per-draft status and overview reconciliation from Datatracker.

`fetch_draft_status` resolves one draft's live draft/IESG state into an
agenda-eligibility signal (backs the `draft_status` read tool);
`reconcile_active_drafts` cross-checks a gather cache's active-draft list
against live Datatracker in both directions (advanced past the WG / revived).
Both share the state-URI resolution and eligibility derivation here, and go
through the write-free `cache._cached_json`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..gather.sources.citations import normalize_draft_name
from .cache import _API_BASE, _DT_BASE, _cached_json, _now_utc, _parse_utc


@dataclass
class DraftStatus:
    """Live Datatracker status for one draft, with a derived agenda signal."""

    name: str
    found: bool
    rev: Optional[str]
    draft_state: Optional[str]  # Active / Expired / Replaced / RFC
    iesg_state: Optional[str]  # I-D Exists / AD Evaluation / RFC Ed Queue / …
    expires: Optional[str]
    intended_status: Optional[str]
    rfc_number: Optional[str]
    eligibility: str  # in-wg / in-iesg / published / dead / unknown
    note: Optional[str] = None


def _resolve_name_uri(uri: Any) -> Optional[str]:
    """Resolve a Datatracker `/api/v1/name/...` URI to its `name` field."""
    if not isinstance(uri, str) or not uri.startswith("/"):
        return uri if isinstance(uri, str) and uri else None
    body, _ = _cached_json(f"{_DT_BASE}{uri}?format=json")
    if not body:
        return None
    value = body.get("name")
    return value if isinstance(value, str) else None


def _state_slug_and_name(state_uri: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a doc `states[]` URI to `(type_slug, state_name)`.

    The state object's `type` is *itself* a URI that must be resolved to a
    slug — a naive `type == "draft"` comparison silently matches nothing.
    """
    body, _ = _cached_json(f"{_DT_BASE}{state_uri}?format=json")
    if not body:
        return None, None
    state_name = body.get("name")
    type_ref = body.get("type")
    slug: Optional[str] = None
    if isinstance(type_ref, str) and type_ref.startswith("/"):
        type_body, _ = _cached_json(f"{_DT_BASE}{type_ref}?format=json")
        if type_body:
            raw_slug = type_body.get("slug")
            slug = raw_slug if isinstance(raw_slug, str) else None
    elif isinstance(type_ref, str):
        slug = type_ref
    return slug, (state_name if isinstance(state_name, str) else None)


def _is_past(expires: Optional[str]) -> bool:
    """True if an ISO `expires` timestamp is in the past."""
    parsed = _parse_utc(expires or "")
    return parsed is not None and parsed < _now_utc()


def _derive_eligibility(
    draft_state: Optional[str],
    iesg_state: Optional[str],
    expires: Optional[str],
    rfc_number: Optional[str],
) -> str:
    """Collapse the raw states into the agenda-eligibility signal.

    published → past the WG, has an RFC number or a `RFC` draft state.
    dead → expired or replaced. in-iesg → any IESG processing state beyond
    `I-D Exists`. in-wg → `I-D Exists` or an active draft with no IESG state.
    """
    ds = (draft_state or "").strip().lower()
    iesg = (iesg_state or "").strip().lower()
    if rfc_number or ds == "rfc":
        return "published"
    if ds in ("expired", "replaced", "repl") or iesg == "dead":
        return "dead"
    if _is_past(expires):
        return "dead"
    if iesg and iesg != "i-d exists":
        return "in-iesg"
    if iesg == "i-d exists" or ds == "active":
        return "in-wg"
    return "unknown"


def _classify_states(
    doc: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], List[str]]:
    """Resolve a doc's `states` URIs to `(draft_state, iesg_state, raw_uris)`.

    Shared by `fetch_draft_status` and `reconcile_active_drafts`. The state
    *objects* (e.g. the one "Active" draft-state) are shared across every
    draft, so the TTL cache makes this nearly free after the first resolve.
    """
    states = [uri for uri in (doc.get("states") or []) if isinstance(uri, str)]
    draft_state: Optional[str] = None
    iesg_state: Optional[str] = None
    for uri in states:
        slug, state_name = _state_slug_and_name(uri)
        if slug == "draft":
            draft_state = state_name
        elif slug == "draft-iesg":
            iesg_state = state_name
    return draft_state, iesg_state, states


def fetch_draft_status(name: str) -> Tuple[Optional[DraftStatus], datetime.datetime]:
    """Live status for one draft. `(None, fetched_at)` if the doc is unknown.

    Honours two Datatracker gotchas: a state's `type` is a URI to resolve to
    a slug, and the `states` list is occasionally returned empty by a flaky
    serialiser — so an empty `states` is *not* treated as authoritative; the
    `expires`/`rfc_number` fields corroborate and a note flags the gap.
    """
    canonical = normalize_draft_name(name)
    doc, fetched = _cached_json(f"{_API_BASE}/doc/document/{canonical}/?format=json")
    if doc is None or not doc.get("name"):
        return None, fetched

    rev = doc.get("rev")
    expires = doc.get("expires")
    rfc_number = doc.get("rfc_number")
    intended = _resolve_name_uri(doc.get("intended_std_level"))
    draft_state, iesg_state, states = _classify_states(doc)

    note: Optional[str] = None
    if not states:
        note = (
            "Datatracker returned no states for this draft (its serialiser is "
            "occasionally flaky here); status is corroborated from the expiry "
            "date and RFC number. Re-run to confirm."
        )

    rfc_str = str(rfc_number) if rfc_number else None
    return (
        DraftStatus(
            name=canonical,
            found=True,
            rev=(str(rev) if rev not in (None, "") else None),
            draft_state=draft_state,
            iesg_state=iesg_state,
            expires=(expires if isinstance(expires, str) else None),
            intended_status=intended,
            rfc_number=rfc_str,
            eligibility=_derive_eligibility(draft_state, iesg_state, expires, rfc_str),
            note=note,
        ),
        fetched,
    )


@dataclass
class DraftReconciliation:
    """How the gather cache's active-draft list diverges from Datatracker."""

    advanced: List[Tuple[str, str]]  # listed active here, but past the WG live
    revived: List[Tuple[str, str]]  # active adopted draft live, absent here
    checked: int  # how many of the cache's active drafts were verified


def _iter_group_adopted_drafts(
    wg: str,
) -> Tuple[List[Dict[str, Any]], datetime.datetime]:
    """All `draft-ietf-<wg>-*` documents Datatracker associates with the group.

    Pages `meta.next` (bounded), keeping only adopted WG drafts so the
    individual drafts a group also touches don't leak in. Each object carries
    `name`/`expires` directly, so no per-doc state resolution is needed for
    the active-or-not check the caller makes.
    """
    prefix = f"draft-ietf-{wg}-"
    objects: List[Dict[str, Any]] = []
    url: Optional[str] = (
        f"{_API_BASE}/doc/document/?group__acronym={wg}&type=draft&limit=200&format=json"
    )
    fetched = _now_utc()
    pages = 0
    while url and pages < 20:
        body, fetched = _cached_json(url)
        if not body:
            break
        for obj in body.get("objects") or []:
            if isinstance(obj, dict) and str(obj.get("name") or "").startswith(prefix):
                objects.append(obj)
        nxt = (body.get("meta") or {}).get("next")
        url = f"{_DT_BASE}{nxt}" if isinstance(nxt, str) and nxt else None
        pages += 1
    return objects, fetched


def reconcile_active_drafts(
    wg: str, active_names: List[str]
) -> Tuple[DraftReconciliation, datetime.datetime]:
    """Cross-check the cache's active-draft list against live Datatracker.

    Two divergences, in both directions:
    - **advanced** — a draft the cache still lists active that Datatracker has
      moved past the WG (IESG processing), published, or expired/replaced.
    - **revived** — an adopted WG draft genuinely still **in the WG** on
      Datatracker (`in-wg`) that the cache's active list omits (typically a
      draft whose cached snapshot expired and was then revived).

    Both directions are derived from a single paged listing of the group's
    adopted drafts — whose objects already carry `states`/`expires`/`rfc_number`
    — so eligibility comes from the listing, not a doc fetch per draft (the
    state objects are shared across drafts and TTL-cached). The revived check
    requires genuine `in-wg` eligibility, not just a future `expires`: an
    adopted draft that aged out of the cache but has *advanced past the WG*
    must read as "drop it", never as a revived draft to agenda.
    """
    active_set = {normalize_draft_name(name) for name in active_names}
    drafts, fetched = _iter_group_adopted_drafts(wg)

    advanced: List[Tuple[str, str]] = []
    revived: List[Tuple[str, str]] = []
    for obj in drafts:
        name = normalize_draft_name(str(obj.get("name") or ""))
        draft_state, iesg_state, _ = _classify_states(obj)
        rfc_number = obj.get("rfc_number")
        rfc_str = str(rfc_number) if rfc_number else None
        eligibility = _derive_eligibility(
            draft_state, iesg_state, obj.get("expires"), rfc_str
        )
        if name in active_set:
            if eligibility in ("in-iesg", "published", "dead"):
                label = iesg_state or draft_state or eligibility
                advanced.append((name, f"{label} ({eligibility})"))
        elif eligibility == "in-wg":
            expires = obj.get("expires")
            when = expires[:10] if isinstance(expires, str) else "?"
            revived.append((name, when))

    return (
        DraftReconciliation(
            advanced=sorted(set(advanced)),
            revived=sorted(set(revived)),
            checked=len(active_set),
        ),
        fetched,
    )
