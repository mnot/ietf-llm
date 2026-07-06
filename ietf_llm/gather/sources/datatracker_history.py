"""Fetch governance / lifecycle events for a WG from Datatracker.

Three event sources, all best-effort against the public JSON API:

  - **Group events** (`/api/v1/group/groupevent/`) — charter approvals,
    state changes, formal milestones. Charter events ALWAYS appear in
    the timeline regardless of the `--months` window (foundational
    context); other group events respect the window.
  - **Role history** (`/api/v1/group/rolehistory/`) — chair / AD
    appointments and changes. ALWAYS included regardless of `--months`,
    since who chaired the WG and when is permanent context.
  - **Document events** (`/api/v1/doc/docevent/`) — adoption, WGLC
    announcement, IESG submission, RFC publication. These are
    per-document and high-volume; they respect the `--months` cutoff.

The output type is the shared `Event` dataclass from
`digest.events`, so the events feed straight into the timeline
digest's existing render path.

Failure modes are deliberate: any HTTP or parsing failure returns an
empty list for that source rather than raising. The gather pipeline
already tolerates Datatracker being unreachable (see `people.py`).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...digest.events import Event
from ...utils import LogLevel, Verbosity, log
from .datatracker import _get_json

_API_BASE = "https://datatracker.ietf.org/api/v1"

#: Datatracker timestamps look like "2024-09-12T14:21:33". They lack
#: tz suffix; the database stores UTC, so we attach it on parse.
_DT_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

#: Map Datatracker DocEvent type slugs to our event_kind taxonomy.
#: Anything not in this map is dropped — there are dozens of slugs
#: (added_comment, changed_action_holders, …) that aren't useful in a
#: condensed timeline. We surface the lifecycle bookends only.
_DOC_EVENT_KINDS = {
    "started_iesg_process": "doc-iesg",
    "iesg_approved": "doc-iesg",
    "published_rfc": "doc-rfc",
    # Document adoption by a WG arrives as a "changed_state" event in
    # practice; the handler resolves that via the `desc` field rather
    # than the type slug.
}

#: GroupEvent type slugs we surface. Most are housekeeping; we want
#: the milestones a reader would want to see.
_GROUP_EVENT_KINDS = {
    "changed_state": "group-state",
    # Charter events come in via several slugs over Datatracker history;
    # we resolve them defensively in _classify_group_event.
}


def fetch_group_events(
    wg: str, months: int, verbose: Verbosity = Verbosity.STATUS
) -> List[Event]:
    """Group-level events: charter approvals, state changes.

    Charter events are returned regardless of `months` (foundational);
    other event kinds are filtered to the last `months` months.
    """
    body = _get_json(f"{_API_BASE}/group/groupevent/?group__acronym={wg}&limit=200")
    if not body or "objects" not in body:
        log(
            f"No groupevent data for {wg} from Datatracker.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    cutoff = _cutoff(months)
    out: List[Event] = []
    for obj in body["objects"]:
        when = _parse_dt_time(obj.get("time"))
        if when is None:
            continue
        event_kind, title = _classify_group_event(obj)
        if event_kind is None:
            continue
        # Policy (2): charter events skip the cutoff; others respect it.
        always_include = event_kind == "charter-approved"
        if not always_include and when < cutoff:
            continue
        out.append(
            Event(
                when=when,
                kind=event_kind,
                title=title,
                detail=None,
                link=None,
            )
        )
    return out


def fetch_role_history(wg: str, verbose: Verbosity = Verbosity.STATUS) -> List[Event]:
    """Chair (and other leadership) appointments through history.

    Always returns the full history — chair appointments are permanent
    context, and there are usually only a handful per WG.
    """
    body = _get_json(f"{_API_BASE}/group/rolehistory/?group__acronym={wg}&limit=200")
    if not body or "objects" not in body:
        log(
            f"No rolehistory data for {wg} from Datatracker.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    out: List[Event] = []
    for obj in body["objects"]:
        role_url = obj.get("name") or ""
        role_slug = _slug_from_url(role_url, "rolename")
        # Chairs are the universally interesting role; ADs cycle for
        # all WGs in the area so listing them here is noise. Tweak
        # this set if other roles prove useful in practice.
        if role_slug != "chair":
            continue
        when = _parse_dt_time(obj.get("time"))
        if when is None:
            continue
        person_name = _person_name(obj.get("person"))
        if not person_name:
            continue
        out.append(
            Event(
                when=when,
                kind="chair-appointed",
                title=f"Chair appointed: {person_name}",
                detail=None,
                link=None,
            )
        )
    return out


def fetch_doc_events(
    wg: str, months: int, verbose: Verbosity = Verbosity.STATUS
) -> List[Event]:
    """Document lifecycle events for every doc in the WG.

    Respects the `months` cutoff — long-running WGs accumulate hundreds
    of document events and we'd flood the digest without it.
    """
    cutoff = _cutoff(months)
    # `time__gte` filters server-side so we don't drag down a full
    # group's history just to discard most of it.
    cutoff_param = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    # Page through `meta.next`: a long-running WG's in-window history can
    # exceed one 500-row page, and a single capped request would silently drop
    # the overflow (likely the newest events, given default ordering).
    path: Optional[str] = (
        f"{_API_BASE}/doc/docevent/"
        f"?doc__group__acronym={wg}"
        f"&time__gte={cutoff_param}"
        "&limit=500"
    )
    objects: List[Dict[str, Any]] = []
    while path:
        body = _get_json(path)
        if not body:
            break
        objects.extend(body.get("objects") or [])
        path = (body.get("meta") or {}).get("next") or None
    if not objects:
        log(
            f"No docevent data for {wg} from Datatracker.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    out: List[Event] = []
    for obj in objects:
        when = _parse_dt_time(obj.get("time"))
        if when is None or when < cutoff:
            continue
        event_kind, title = _classify_doc_event(obj)
        if event_kind is None:
            continue
        out.append(
            Event(
                when=when,
                kind=event_kind,
                title=title,
                detail=None,
                link=None,
            )
        )
    return out


# --- Classification helpers -----------------------------------------------


def _classify_group_event(obj: Dict[str, Any]) -> tuple[Optional[str], str]:
    """Map a GroupEvent JSON object to (kind, title) or (None, '').

    Three signals to read:
      - `type` slug ("changed_state", "added_comment", ...)
      - `desc` (free-text description, e.g. "Charter approved by IESG")
      - referenced state (when type == changed_state)
    """
    type_slug = (obj.get("type") or "").strip()
    desc = (obj.get("desc") or "").strip()
    # Charter events: Datatracker emits a few different forms over its
    # history. Recognise by description rather than relying on a single
    # type slug.
    desc_lower = desc.lower()
    if "charter approved" in desc_lower or "charter changed" in desc_lower:
        return ("charter-approved", desc or "Charter event")
    if type_slug == "changed_state":
        if "state changed" in desc_lower or desc:
            return ("group-state", desc or "Group state changed")
        return ("group-state", "Group state changed")
    return (None, "")


def _classify_doc_event(obj: Dict[str, Any]) -> tuple[Optional[str], str]:
    """Map a DocEvent JSON object to (kind, title) or (None, '')."""
    type_slug = (obj.get("type") or "").strip()
    desc = (obj.get("desc") or "").strip()
    doc_url = obj.get("doc") or ""
    doc_name = _slug_from_url(doc_url, "document") or "(document)"
    if type_slug == "published_rfc":
        return ("doc-rfc", f"`{doc_name}` published as RFC")
    if type_slug in ("started_iesg_process", "iesg_approved"):
        action = (
            "submitted to IESG"
            if type_slug == "started_iesg_process"
            else "approved by IESG"
        )
        return ("doc-iesg", f"`{doc_name}` {action}")
    # Adoption by WG is recorded as a state change with a recognisable
    # description; pattern-match defensively.
    desc_lower = desc.lower()
    if "adopted by" in desc_lower or "adopted as a working group" in desc_lower:
        return ("doc-adopted", f"`{doc_name}` adopted by WG")
    # WGLC announcement event (some Datatracker eras use this slug, some
    # surface it via desc). Prefer Datatracker over the mailing-list
    # heuristic when we find it here.
    if (
        type_slug == "sent_last_call"
        or "last call" in desc_lower
        and "issued" in desc_lower
    ):
        return ("doc-wglc", f"`{doc_name}`: WG Last Call issued")
    return (None, "")


# --- Helpers --------------------------------------------------------------


def _cutoff(months: int) -> datetime:
    """tz-aware cutoff `months` months before now. 30-day approximation
    is fine for digest purposes (events near the boundary are not
    semantically different from events just inside)."""
    return datetime.now(timezone.utc) - timedelta(days=30 * max(1, months))


def _parse_dt_time(value: Any) -> Optional[datetime]:
    """Parse a Datatracker `time` field to a tz-aware UTC datetime."""
    if not isinstance(value, str):
        return None
    match = _DT_TIMESTAMP_RE.match(value)
    if not match:
        return None
    try:
        parsed = datetime.fromisoformat(match.group(1))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


_URL_SLUG_RE = re.compile(r"/api/v1/(?:[^/]+/)?([^/]+)/([^/]+)/?$")


def _slug_from_url(url: str, expected_section: Optional[str] = None) -> Optional[str]:
    """Extract the final path segment from a Datatracker reference URL.

    e.g. `/api/v1/doc/document/draft-ietf-aipref-vocab/` → `draft-ietf-aipref-vocab`.
    Optional `expected_section` lets the caller assert the URL really
    is the kind they think it is.
    """
    if not isinstance(url, str):
        return None
    match = _URL_SLUG_RE.search(url)
    if not match:
        return None
    if expected_section and match.group(1) != expected_section:
        return None
    return match.group(2)


def _person_name(person_url: Optional[str]) -> Optional[str]:
    """Resolve a Datatracker person URL to a display name. One extra
    HTTP call per chair appointment — acceptable; there are few chairs.
    """
    if not person_url:
        return None
    body = _get_json(person_url)
    if not body:
        return None
    name = body.get("name") or body.get("ascii")
    return str(name) if name else None
