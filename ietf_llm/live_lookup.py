"""Live Datatracker fact lookups for the chair-workflow read tools.

This module is the one read-path exception to the server's offline rule.
`meeting_sessions` and `draft_status` hit Datatracker *live* because the
facts they serve — meeting schedules, IESG document states — change daily
and a gather cache (a 36-month window, often days stale) is too coarse for
an agenda. They are therefore gated exactly like the gather tools
(registered only when `freshness.gather_enabled()` is true: on for a local
stdio server, off for the shared read-only HTTP replica) and imported
lazily by `mcp_server`, so the default read path never pulls this in.

Unlike the gather layer it keeps only a small TTL cache — in-process, plus a
best-effort cross-process copy on disk (`.live-cache.json` under the cache
root). The disk copy exists so a cold, short-lived process — an
`ietf-llm-query` invocation, which starts with an empty in-process cache —
does not re-hit Datatracker on every call; without it a skill shelling out
repeatedly would hammer the API. It writes nothing else: no ETag store, and
never any corpus content, so a re-gather is still the only thing that mutates
a corpus. On a read-only cache mount the disk write silently no-ops. Every
result carries the UTC time the underlying data was fetched, so a caller can
see how fresh (or how stale-on-failure) it is; the tool wrappers render that
via `age_stamp`.

The data source is always the Datatracker REST API / agenda JSON, never a
scraped HTML page (see `docs/architecture.md`, "Use the Datatracker API").
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .gather.citations import normalize_draft_name
from .gather.meetings import _uri_id
from .utils import (
    DEFAULT_HEADERS,
    atomic_open,
    file_lock,
    get_cache_dir,
    governed_get,
)

_DT_BASE = "https://datatracker.ietf.org"
_API_BASE = f"{_DT_BASE}/api/v1"
_MEETECHO = "https://meetings.conf.meetecho.com"

#: Default TTL for the in-process live cache (seconds). Short — these facts
#: move daily, and the point of the cache is only to coalesce a burst of
#: reads in one agenda-building session, not to serve stale data. Override
#: with `IETF_LLM_LIVE_TTL`.
_DEFAULT_TTL = 300.0


def _ttl_seconds() -> float:
    """The live cache TTL from `IETF_LLM_LIVE_TTL` (seconds; default 300).

    A non-numeric or negative value falls back to the default.
    """
    raw = os.environ.get("IETF_LLM_LIVE_TTL", "").strip()
    if not raw:
        return _DEFAULT_TTL
    try:
        ttl = float(raw)
    except ValueError:
        return _DEFAULT_TTL
    return ttl if ttl >= 0 else _DEFAULT_TTL


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class _CacheEntry:
    body: Optional[Dict[str, Any]]
    fetched_at: datetime.datetime
    monotonic: float


_cache: Dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _fetch_json(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """GET a Datatracker JSON URL, returning the decoded dict or None.

    No disk cache and no ETag store — the read path writes nothing. Uses the
    shared rate governor (`governed_get`) so a burst of agenda lookups stays
    polite to Datatracker. Any transport or decode error is swallowed to
    None; the caller decides whether to fall back to a stale cache entry.
    """
    try:
        response = governed_get(url, headers=dict(DEFAULT_HEADERS), timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _live_cache_path() -> str:
    return os.path.join(get_cache_dir(), ".live-cache.json")


def _disk_get(
    url: str, ttl: float
) -> Optional[Tuple[Optional[Dict[str, Any]], datetime.datetime]]:
    """A fresh (within-TTL) disk-cached `(body, fetched_at)` for `url`, or None.
    Best-effort: any read/parse error yields None (treat as a miss)."""
    try:
        with open(_live_cache_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = data.get(url) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    epoch = entry.get("epoch")
    if not isinstance(epoch, (int, float)) or (time.time() - epoch) >= ttl:
        return None
    try:
        fetched_at = datetime.datetime.fromisoformat(str(entry.get("fetched_at")))
    except ValueError:
        fetched_at = _now_utc()
    body = entry.get("body")
    return (body if isinstance(body, dict) else None), fetched_at


def _disk_put(
    url: str, body: Dict[str, Any], fetched_at: datetime.datetime, ttl: float
) -> None:
    """Store a fresh entry on disk, pruning expired ones. Best-effort and
    serialised across processes by a file lock so concurrent CLI invocations
    do not clobber each other; any OS error (e.g. a read-only mount) no-ops."""
    path = _live_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with file_lock(path + ".lock"):
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            now = time.time()
            fresh = {
                key: val
                for key, val in data.items()
                if isinstance(val, dict)
                and isinstance(val.get("epoch"), (int, float))
                and (now - val["epoch"]) < ttl
            }
            fresh[url] = {
                "body": body,
                "fetched_at": fetched_at.isoformat(),
                "epoch": now,
            }
            with atomic_open(path) as handle:
                json.dump(fresh, handle)
    except OSError:
        pass


def _cached_json(url: str) -> Tuple[Optional[Dict[str, Any]], datetime.datetime]:
    """Return `(body, fetched_at)` for `url`, fetching at most once per TTL.

    Checks the in-process cache first, then the cross-process disk cache (so a
    cold CLI process reuses a recent fetch rather than re-hitting Datatracker),
    then fetches. On a fetch failure a previously-cached body is returned with
    its *original* timestamp (stale, but the age stamp will say so, and a stale
    answer beats no answer); with no prior entry, `(None, now)`.
    """
    ttl = _ttl_seconds()
    now_mono = time.monotonic()
    with _cache_lock:
        entry = _cache.get(url)
        if entry is not None and (now_mono - entry.monotonic) < ttl:
            return entry.body, entry.fetched_at
    disk = _disk_get(url, ttl)
    if disk is not None:
        disk_body, disk_fetched = disk
        with _cache_lock:
            _cache[url] = _CacheEntry(
                body=disk_body, fetched_at=disk_fetched, monotonic=time.monotonic()
            )
        return disk_body, disk_fetched
    body = _fetch_json(url)
    if body is None:
        with _cache_lock:
            entry = _cache.get(url)
        if entry is not None:
            return entry.body, entry.fetched_at
        return None, _now_utc()
    fetched_at = _now_utc()
    with _cache_lock:
        _cache[url] = _CacheEntry(
            body=body, fetched_at=fetched_at, monotonic=time.monotonic()
        )
    _disk_put(url, body, fetched_at, ttl)
    return body, fetched_at


def _reset_cache() -> None:
    """Drop the in-process cache. For tests."""
    with _cache_lock:
        _cache.clear()


def age_stamp(fetched_at: datetime.datetime) -> str:
    """A one-line freshness footer for a live result.

    Reports the UTC fetch time and how long ago it was, so a caller can tell
    a just-fetched answer from one served out of the short TTL cache (or a
    stale-on-failure fallback).
    """
    age = max(0, int((_now_utc() - fetched_at).total_seconds()))
    stamp = fetched_at.strftime("%Y-%m-%d %H:%M:%SZ")
    if age <= 1:
        return f"_Live from Datatracker, fetched just now ({stamp})._"
    return (
        f"_Live from Datatracker, fetched {age}s ago ({stamp}); "
        f"cached up to {int(_ttl_seconds())}s._"
    )


# --------------------------------------------------------------------------
# Meeting session logistics (item 1)
# --------------------------------------------------------------------------


def is_interim_number(meeting: str) -> bool:
    """True if `meeting` is an interim id (`interim-2026-aipref-05`) rather
    than a numbered IETF meeting (`126`). Datatracker keys both kinds the
    same way in `agenda.json`, but the Meetecho URL and label differ."""
    return (meeting or "").strip().lower().startswith("interim-")


def meeting_label(meeting: str) -> str:
    """Human label for a meeting id: `IETF 126` for a numbered meeting, the
    canonical (lowercase) interim id (`interim-2026-aipref-05`) otherwise."""
    meeting = (meeting or "").strip()
    return meeting.lower() if is_interim_number(meeting) else f"IETF {meeting}"


@dataclass
class MeetingSession:
    """One group session at a meeting, rendered for an agenda."""

    date: str  # venue-local weekday + date, e.g. "Friday 18 July 2026"
    start_local: str  # venue-local "HH:MM"
    end_local: str
    tz: str  # IANA zone, e.g. "Europe/Vienna"
    tz_abbrev: str  # local abbreviation/offset, e.g. "CEST" or "+07"
    start_utc: str  # "YYYY-MM-DDTHH:MMZ"
    room: Optional[str]
    session_id: str
    meetecho_full: str  # numbered meetings; "" for interims (no onsite/remote split)
    meetecho_onsite: str
    agenda_url: Optional[str]
    minutes_url: Optional[str]
    # Interims carry the connection details (a Meetecho URL, a Teams note,
    # "in-room", …) in the agenda's free-text `remote_instructions`; numbered
    # meetings use the constructed Meetecho URLs above instead.
    remote_instructions: Optional[str] = None


def _parse_utc(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO timestamp to a tz-aware UTC datetime (Z or naive = UTC)."""
    raw = (value or "").strip().replace("Z", "+00:00")
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _parse_duration(value: str) -> datetime.timedelta:
    """Parse a `H:MM:SS` (or `H:MM`) duration; unparseable → zero."""
    parts = (value or "").split(":")
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return datetime.timedelta()
    while len(nums) < 3:
        nums.append(0)
    return datetime.timedelta(hours=nums[0], minutes=nums[1], seconds=nums[2])


def _resolve_zone(tz_name: Optional[str]) -> Optional[datetime.tzinfo]:
    """Resolve an IANA zone name to a tzinfo, or None if unavailable.

    Returns None when the name is missing or the platform has no tz database
    entry for it (missing `tzdata`); callers then fall back to UTC and label
    it so a reader is never shown a wrong local time presented as right.
    """
    if not tz_name:
        return None
    # Imported here so a platform without zoneinfo never breaks import.
    from zoneinfo import (  # pylint: disable=import-outside-toplevel
        ZoneInfo,
        ZoneInfoNotFoundError,
    )

    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _meeting_timezone(meeting: str) -> Optional[str]:
    """The venue IANA timezone for a meeting, or None.

    The `?number=` filter accepts an interim id as well as a numbered
    meeting, so this serves both (a virtual interim's zone is usually UTC).
    """
    body, _ = _cached_json(f"{_API_BASE}/meeting/meeting/?number={meeting}&format=json")
    if not body:
        return None
    objects = body.get("objects") or []
    if not objects:
        return None
    tz_name = objects[0].get("time_zone")
    return tz_name if isinstance(tz_name, str) and tz_name else None


def _build_session(
    meeting: str,
    raw: Dict[str, Any],
    tz_name: Optional[str],
    zone: Optional[datetime.tzinfo],
    is_interim: bool = False,
) -> MeetingSession:
    start_utc = _parse_utc(str(raw.get("start") or ""))
    end_utc = (
        start_utc + _parse_duration(str(raw.get("duration") or ""))
        if start_utc
        else None
    )
    start_local: Optional[datetime.datetime]
    end_local: Optional[datetime.datetime]
    if start_utc and zone is not None:
        start_local = start_utc.astimezone(zone)
        end_local = end_utc.astimezone(zone) if end_utc else None
        tz_abbrev = start_local.tzname() or (tz_name or "")
        tz_iana = tz_name or ""
    else:
        # No usable zone: present UTC and label it honestly.
        start_local = start_utc
        end_local = end_utc
        tz_abbrev = "UTC" if start_utc else ""
        tz_iana = tz_name or "UTC"
    session_id = str(raw.get("session_id") or "")
    # Numbered meetings get the constructed remote/onsite Meetecho pair; an
    # interim has no onsite room and may not be on Meetecho at all, so we
    # surface its agenda's `remote_instructions` (a Meetecho URL, a Teams
    # note, …) verbatim instead of guessing a URL.
    return MeetingSession(
        date=start_local.strftime("%A %d %B %Y") if start_local else "",
        start_local=start_local.strftime("%H:%M") if start_local else "",
        end_local=end_local.strftime("%H:%M") if end_local else "",
        tz=tz_iana,
        tz_abbrev=tz_abbrev,
        start_utc=start_utc.strftime("%Y-%m-%dT%H:%MZ") if start_utc else "",
        room=(raw.get("location") or None),
        session_id=session_id,
        meetecho_full=(
            f"{_MEETECHO}/ietf{meeting}/?session={session_id}"
            if session_id and not is_interim
            else ""
        ),
        meetecho_onsite=(
            f"{_MEETECHO}/onsite{meeting}/?session={session_id}"
            if session_id and not is_interim
            else ""
        ),
        agenda_url=(raw.get("agenda") or None),
        minutes_url=(raw.get("minutes") or None),
        remote_instructions=(
            (raw.get("remote_instructions") or None) if is_interim else None
        ),
    )


def fetch_meeting_sessions(
    corpus: str, meeting: str
) -> Tuple[List[MeetingSession], datetime.datetime, Optional[str]]:
    """All of `corpus`'s sessions at `meeting`, venue-local.

    `meeting` is a numbered IETF meeting (`126`) or an interim id
    (`interim-2026-aipref-05`); Datatracker keys both the same way in
    `agenda.json`, so the only difference is the Meetecho/label handling in
    `_build_session`.

    Returns `(sessions, fetched_at, error)`. `error` is a human message when
    the agenda could not be read or has no published rows yet; `sessions` is
    then empty. An *empty* list with no error means the group simply has no
    session at that meeting.
    """
    meeting = str(meeting).strip()
    is_interim = is_interim_number(meeting)
    # Datatracker keys interim agendas by the lowercase id; normalise so a
    # mixed-case id (which passes the case-insensitive validation) still
    # resolves rather than 404-ing as "no agenda".
    if is_interim:
        meeting = meeting.lower()
    label = meeting_label(meeting)
    agenda, fetched = _cached_json(f"{_DT_BASE}/meeting/{meeting}/agenda.json")
    if agenda is None:
        return (
            [],
            fetched,
            f"Could not fetch the agenda for {label} from Datatracker "
            "(no published agenda yet, or the meeting id is wrong).",
        )
    rows = agenda.get(meeting)
    if not isinstance(rows, list):
        return [], fetched, f"{label} has no published agenda yet."
    tz_name = _meeting_timezone(meeting)
    zone = _resolve_zone(tz_name)
    sessions = [
        _build_session(meeting, raw, tz_name, zone, is_interim)
        for raw in rows
        if isinstance(raw, dict) and (raw.get("group") or {}).get("acronym") == corpus
    ]
    return sessions, fetched, None


@dataclass
class UpcomingMeeting:
    """A meeting `corpus` is scheduled at, for the discovery listing."""

    number: str  # "127" or "interim-2026-aipref-05"
    kind: str  # "ietf" | "interim" | ""
    date: str  # "YYYY-MM-DD" (Datatracker meeting date)
    agenda_url: str  # the human agenda page, drillable via `meeting_sessions`


def _resolve_meetings(ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Batch-resolve meeting ids → `{number: (kind, date)}` via `id__in`.

    The read-path twin of `gather.meetings._batch_fetch_meetings`: same
    `id__in` batching, but on the write-free `_cached_json` (not gather's
    on-disk ETag store) and keyed by meeting *number* (the caller builds
    agenda URLs / drill-in calls from it) rather than by uri-id. Keep the
    two in step if Datatracker's meeting shape changes.
    """
    out: Dict[str, Tuple[str, str]] = {}
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), 100):
        chunk = ordered[start : start + 100]
        body, _ = _cached_json(
            f"{_API_BASE}/meeting/meeting/?id__in={','.join(chunk)}"
            "&limit=100&format=json"
        )
        for meeting in (body or {}).get("objects") or []:
            number = meeting.get("number")
            if number is None:
                continue
            kind = (meeting.get("type") or "").rstrip("/").split("/")[-1]
            out[str(number)] = (kind, str(meeting.get("date") or ""))
    return out


def fetch_upcoming_meetings(
    corpus: str,
) -> Tuple[List[UpcomingMeeting], datetime.datetime, Optional[str]]:
    """`corpus`'s upcoming meetings (numbered + interim), soonest first.

    Discovery for `meeting_sessions`: an interim id (`interim-2026-aipref-05`)
    isn't guessable, so this lists the group's future-dated meetings — each
    drillable by passing its `number` back to `fetch_meeting_sessions`. Walks
    the most-recent page of the group's sessions (future meetings carry the
    newest session ids) and batch-resolves their meeting metadata; the read
    stays date-level (cheap), with full per-session times on the drill-in.
    """
    today = _now_utc().date().isoformat()
    body, fetched = _cached_json(
        f"{_API_BASE}/meeting/session/?group__acronym={corpus}"
        "&limit=100&order_by=-id&format=json"
    )
    if body is None:
        return (
            [],
            fetched,
            f"Could not fetch {corpus}'s sessions from Datatracker.",
        )
    ids: List[str] = []
    for sess in body.get("objects") or []:
        uri = sess.get("meeting")
        if uri:
            ids.append(_uri_id(str(uri)))
    meetings = _resolve_meetings(ids)
    upcoming = [
        UpcomingMeeting(
            number=number,
            kind=kind,
            date=date,
            agenda_url=f"{_DT_BASE}/meeting/{number}/agenda",
        )
        for number, (kind, date) in meetings.items()
        if date >= today
    ]
    upcoming.sort(key=lambda m: (m.date, m.number))
    return upcoming, fetched, None


# --------------------------------------------------------------------------
# Per-draft status (item 2)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Overview reconciliation (item 2, second part)
# --------------------------------------------------------------------------


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
