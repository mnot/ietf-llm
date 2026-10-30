"""Mirror every chartered WG and RG from Datatracker into `_groups/`.

The effort catalog (`catalog.py`) holds the active slice only and none of a
group's substance. This holds all of them, concluded ones included, with the
charter text, milestones, and a timeline built from bulk Datatracker feeds.
`singletons.groups` is the read side; this is the only writer.

Cost is kept off the fleet in two ways. Every feed is fetched in bulk and
incrementally (`time__gte` from a stored watermark, rows merged by id), so a
daily refresh is a handful of small requests rather than a per-group fan-out.
And the one per-group fetch, the charter text, is bounded per run and only
re-done when a group's charter revision moves, so a first run trickles in over
several invocations. The seed store ships a populated mirror, which is what
makes the first run cheap for everyone but the publisher.

Best-effort throughout, like the other mirrors: a failure leaves what is
already on disk and logs at PROGRESS.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from ...atomicio import write_if_changed
from ...config import service as service_config
from ...freshness import record_gather
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get, http_error_detail
from ...paths import get_index_dir
from ...singletons import groups as reader
from . import _mirror
from .charter import approved_rev
from .datatracker_history import chair_changes, chair_event_title

_API = "https://datatracker.ietf.org/api/v1"
_PAGE = 1000
_TIMEOUT = 30

#: Charters fetched per run. Active groups go first, so the groups people ask
#: about arrive before the archive of concluded ones.
CHARTER_BATCH = 200

#: Minimum gap between batches while a charter backlog remains, so a first run
#: trickles in over invocations instead of hammering ietf.org in one.
_BATCH_INTERVAL = 15 * 60

#: After a failed group-list refresh, wait this long before trying again.
_FAILURE_BACKOFF = 60 * 60

#: A charter the server says does not exist is not asked for again for this long.
_RETRY_AFTER = 7 * 24 * 60 * 60

#: Responses that mean the revision is not coming back.
_ABSENT_STATUSES = frozenset({404, 410})

#: A charter that fails transiently this many times is set aside for a day, so
#: one that fails every time cannot hold the front of the queue indefinitely.
_MAX_TRIES = 5
_TRANSIENT_LOCKOUT = 24 * 60 * 60

#: A batch stops after this many transient failures in a row. They mean the
#: network or the server is unwell, and carrying on would only add load.
_MAX_CONSECUTIVE_FAILURES = 5

_GROUP_TYPES = "wg,rg"

#: Feed name -> (path, filters), each paged and merged by row id.
_FEEDS: Dict[str, str] = {
    "state-events": f"{_API}/group/groupevent/?type=changed_state&group__type__in={_GROUP_TYPES}",
    "charter-revs": f"{_API}/doc/docevent/?doc__type=charter&type=new_revision",
    "charter-approvals": f"{_API}/doc/docevent/?doc__type=charter&type=iesg_approved",
    # Chair changes: "Chairs changed to <b>A, B</b> from A".
    "chair-events": (
        f"{_API}/group/groupevent/?type=info_changed&desc__startswith=Chair"
        f"&group__type__in={_GROUP_TYPES}"
    ),
    "milestones": f"{_API}/group/groupmilestone/?group__type__in={_GROUP_TYPES}",
    "milestone-history": f"{_API}/group/groupmilestonehistory/?group__type__in={_GROUP_TYPES}",
    "rfc-published": f"{_API}/doc/docevent/?type=published_rfc",
}

_TAG_RE = re.compile(r"<[^>]+>")
_RFC_URI_RE = re.compile(r"/(rfc\d+)/?$")

Fetcher = Callable[[str, Verbosity], Optional[List[Dict[str, Any]]]]


# --- HTTP -------------------------------------------------------------------


def _fetch_all(url: str, verbosity: Verbosity) -> Optional[List[Dict[str, Any]]]:
    """Every object behind a paged Datatracker URL, or None if any page fails.
    All-or-nothing, so a partial result never advances a watermark."""
    objects: List[Dict[str, Any]] = []
    next_url: Optional[str] = f"{url}&limit={_PAGE}&format=json"
    while next_url:
        try:
            response = governed_get(
                next_url, headers=dict(DEFAULT_HEADERS), timeout=_TIMEOUT
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as err:
            log(
                f"Groups: fetch {next_url} failed: {http_error_detail(err)}",
                verbosity,
                LogLevel.PROGRESS,
            )
            return None
        objects.extend(o for o in body.get("objects") or [] if isinstance(o, dict))
        nxt = (body.get("meta") or {}).get("next")
        next_url = f"https://datatracker.ietf.org{nxt}" if nxt else None
    return objects


# --- Incremental state ------------------------------------------------------


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: str, value: Any, verbosity: Verbosity) -> bool:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        with open(path, "rb") as handle:
            if handle.read() == payload:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return _mirror.write_body(path, payload, verbosity, "Groups")


def _slim(feed: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only what the derived files need; raw rows are large."""
    keep = {
        "state-events": ("id", "group", "desc", "time"),
        "chair-events": ("id", "group", "desc", "time"),
        "charter-revs": ("id", "doc", "rev", "time"),
        "charter-approvals": ("id", "doc", "rev", "desc", "time"),
        "milestones": (
            "id",
            "group",
            "desc",
            "due",
            "resolved",
            "state",
            "docs",
            "time",
        ),
        "milestone-history": (
            "id",
            "milestone",
            "group",
            "desc",
            "due",
            "resolved",
            "state",
            "docs",
            "time",
        ),
        "rfc-published": ("id", "doc", "time"),
    }[feed]
    return {k: obj.get(k) for k in keep}


def _refresh_feed(
    feed: str,
    state_path: str,
    watermarks: Dict[str, str],
    fetch: Fetcher,
    verbosity: Verbosity,
) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Merge new rows of `feed` into its stored rows. Returns the rows and
    whether anything changed; a failed fetch keeps the stored rows."""
    rows: Dict[str, Dict[str, Any]] = _read_json(state_path, {})
    url = _FEEDS[feed]
    since = watermarks.get(feed)
    if since and rows:
        url += f"&time__gte={since}"
    fetched = fetch(url, verbosity)
    if fetched is None:
        return rows, False
    changed = False
    newest = since or ""
    for obj in fetched:
        row = _slim(feed, obj)
        key = str(row["id"])
        if rows.get(key) != row:
            rows[key] = row
            changed = True
        newest = max(newest, str(row.get("time") or ""))
    if newest:
        watermarks[feed] = newest[:19]
    return rows, changed


# --- Derivation -------------------------------------------------------------


def _slug(uri: Optional[str]) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1] if uri else ""


def _acronym_of_charter(doc_uri: str) -> str:
    """`/api/v1/doc/document/charter-ietf-httpbis/` -> `httpbis`."""
    name = _slug(doc_uri)
    parts = name.split("-", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "charter" else ""


def _day(stamp: Any) -> str:
    return str(stamp or "")[:10]


def _plain(text: Any) -> str:
    return " ".join(_TAG_RE.sub("", str(text or "")).split())


def build_groups(
    groups: List[Dict[str, Any]],
    areas: List[Dict[str, Any]],
    charters: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Slim, sorted group records. `areas` and `charters` resolve each
    group's parent area and current charter revision."""
    parents = {
        a.get("resource_uri"): (a.get("acronym") or "", a.get("name") or "")
        for a in areas
    }
    revs = {c.get("group"): c for c in charters}
    out: List[Dict[str, Any]] = []
    for obj in groups:
        acronym = obj.get("acronym") or ""
        if not acronym:
            continue
        area, area_name = parents.get(obj.get("parent") or "", ("", ""))
        charter = revs.get(obj.get("resource_uri")) or {}
        rev, status = approved_rev(charter)
        out.append(
            {
                "acronym": acronym,
                "name": obj.get("name") or "",
                "type": _slug(obj.get("type")),
                "state": _slug(obj.get("state")),
                "area": area,
                "area_name": area_name,
                "description": " ".join((obj.get("description") or "").split()),
                "charter_doc": charter.get("name") or "",
                "charter_rev": rev,
                "charter_status": status,
                "charter_draft_rev": charter.get("rev") or "" if status else "",
                "list_email": obj.get("list_email") or "",
                "uri": obj.get("resource_uri") or "",
            }
        )
    out.sort(key=lambda g: g["acronym"])
    return out


def build_milestones(
    rows: Dict[str, Dict[str, Any]], by_uri: Dict[str, str]
) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows.values():
        acronym = by_uri.get(row.get("group") or "")
        if not acronym:
            continue
        out.setdefault(acronym, []).append(
            {
                "id": row["id"],
                "desc": " ".join(str(row.get("desc") or "").split()),
                "due": row.get("due") or "",
                "resolved": row.get("resolved") or "",
                "state": _slug(row.get("state")),
                "docs": [_slug(d) for d in row.get("docs") or []],
            }
        )
    for items in out.values():
        items.sort(key=lambda m: (m["due"] or "9999", m["id"]))
    return out


def _milestone_events(
    history: Dict[str, Dict[str, Any]],
    current: Dict[str, Dict[str, Any]],
    by_uri: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Diff each milestone's successive versions into dated events."""
    versions: Dict[str, List[Dict[str, Any]]] = {}
    for row in history.values():
        versions.setdefault(_slug(row.get("milestone")), []).append(row)
    for row in current.values():
        versions.setdefault(str(row["id"]), []).append(row)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for chain in versions.values():
        chain.sort(key=lambda r: str(r.get("time") or ""))
        acronym = by_uri.get(chain[-1].get("group") or "")
        if not acronym:
            continue
        previous: Optional[Dict[str, Any]] = None
        for row in chain:
            event = _milestone_event(previous, row)
            if event:
                out.setdefault(acronym, []).append(event)
            previous = row
    return out


def _milestone_event(
    prev: Optional[Dict[str, Any]], row: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    desc = " ".join(str(row.get("desc") or "").split())
    day = _day(row.get("time"))
    due = str(row.get("due") or "")
    if prev is None:
        suffix = f" (due {due[:7]})" if due else ""
        return {
            "t": day,
            "kind": "milestone",
            "title": f"Milestone added: {desc}{suffix}",
        }
    state = _slug(row.get("state"))
    resolved = str(row.get("resolved") or "").strip()
    if state == "deleted" and _slug(prev.get("state")) != "deleted":
        return {"t": day, "kind": "milestone", "title": f"Milestone dropped: {desc}"}
    if resolved and not str(prev.get("resolved") or "").strip():
        return {
            "t": day,
            "kind": "milestone",
            "title": f"Milestone {resolved.lower()}: {desc}",
        }
    if due and due != str(prev.get("due") or ""):
        was = str(prev.get("due") or "")[:7]
        moved = f" (was {was})" if was else ""
        return {
            "t": day,
            "kind": "milestone",
            "title": f"Milestone due {due[:7]}{moved}: {desc}",
        }
    return None


def build_timeline(
    feeds: Dict[str, Dict[str, Dict[str, Any]]],
    by_uri: Dict[str, str],
    rfc_lookup: Callable[[str], Optional[Tuple[str, str]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Dated events per group, oldest first."""
    out: Dict[str, List[Dict[str, Any]]] = {}

    def add(acronym: str, when: Any, kind: str, title: str) -> None:
        if acronym and when:
            out.setdefault(acronym, []).append(
                {"t": _day(when), "kind": kind, "title": title}
            )

    for row in feeds["state-events"].values():
        add(
            by_uri.get(row.get("group") or "", ""),
            row.get("time"),
            "group-state",
            _plain(row.get("desc")),
        )
    for row in feeds["chair-events"].values():
        changes = chair_changes({"type": "info_changed", "desc": row.get("desc")})
        for kind, name in changes:
            add(
                by_uri.get(row.get("group") or "", ""),
                row.get("time"),
                kind,
                chair_event_title(kind, name),
            )
    for row in feeds["charter-revs"].values():
        add(
            _acronym_of_charter(row.get("doc") or ""),
            row.get("time"),
            "charter-rev",
            f"Charter revision {row.get('rev')}",
        )
    for row in feeds["charter-approvals"].values():
        text = _plain(row.get("desc")) or "Charter approved"
        add(
            _acronym_of_charter(row.get("doc") or ""),
            row.get("time"),
            "charter-approved",
            f"{text} (rev {row.get('rev')})",
        )
    for acronym, events in _milestone_events(
        feeds["milestone-history"], feeds["milestones"], by_uri
    ).items():
        out.setdefault(acronym, []).extend(events)
    for row in feeds["rfc-published"].values():
        match = _RFC_URI_RE.search(row.get("doc") or "")
        found = rfc_lookup(match.group(1)) if match else None
        if found:
            acronym, title = found
            number = match.group(1)[3:] if match else ""
            add(acronym, row.get("time"), "rfc", f"RFC {number} published: {title}")
    for events in out.values():
        events.sort(key=lambda e: (e["t"], e["kind"], e["title"]))
    return out


def _rfc_lookup() -> Callable[[str], Optional[Tuple[str, str]]]:
    """`rfcN` -> (publishing WG, title) from the offline `_rfc/` mirror."""
    # Lazy: keeps the two singletons' readers independent at import time.
    # pylint: disable-next=import-outside-toplevel
    from ...singletons import rfcs as rfc_reader

    data = rfc_reader._load()  # pylint: disable=protected-access

    def lookup(name: str) -> Optional[Tuple[str, str]]:
        if data is None:
            return None
        entry = data.rfcs.get(name.upper())
        if not entry or not entry.get("wg"):
            return None
        return str(entry["wg"]), str(entry.get("title") or "")

    return lookup


# --- Charter files ----------------------------------------------------------


def render_charter_file(
    group: Dict[str, Any], text: Optional[str], milestones: List[Dict[str, Any]]
) -> str:
    """The document that is embedded: metadata, charter, current milestones.

    Plain text with a `Source:` line the chunker lifts as the citation URL.
    A group without charter text carries its Datatracker description, so it
    is still searchable.
    """
    acronym = group["acronym"]
    doc = group.get("charter_doc") or ""
    lines = [
        f"# {acronym} — {group.get('name', '')}",
        "",
        f"Type: {reader.type_label(group)}",
        f"State: {group.get('state', '')}",
    ]
    if group.get("area_name"):
        lines.append(f"Area: {group['area_name']} ({group.get('area', '')})")
    if doc:
        lines.append(f"Source: https://datatracker.ietf.org/doc/{doc}/")
    if group.get("charter_status") == "unapproved":
        lines.append("Note: this charter was never approved.")
    lines.append("")
    if text:
        lines += [reader.CHARTER_HEADING, "", text.strip()]
    else:
        lines += ["## Description", "", group.get("description", "").strip()]
    live = [m for m in milestones if m.get("state") != "deleted"]
    if live:
        lines += ["", reader.MILESTONES_HEADING, ""]
        # Dated against the epoch so the embedded text does not change merely
        # because a milestone slipped into the past.
        lines += [f"- {reader.milestone_line(m, date.min)}" for m in live]
    return "\n".join(lines) + "\n"


def _fetch_charter(doc: str, rev: str) -> Optional[str]:
    """The text of one charter revision.

    Three outcomes, because the caller treats them differently: the text; `""`
    when the server says the revision does not exist (404, or 410 Gone), which
    is permanent and worth remembering; and None for anything else (network error, throttling,
    a server error, an empty body), which is transient and must not be.
    """
    url = f"https://www.ietf.org/charter/{doc}-{rev}.txt"
    try:
        response = governed_get(url, headers=dict(DEFAULT_HEADERS), timeout=_TIMEOUT)
    except requests.RequestException as err:
        log(
            f"Groups: fetch {url} failed: {http_error_detail(err)}", level=LogLevel.WARN
        )
        return None
    if response.status_code in _ABSENT_STATUSES:
        return ""
    if not response.ok or not response.text:
        log(f"Groups: fetch {url} returned {response.status_code}", level=LogLevel.WARN)
        return None
    return response.text


def _is_pending(have: Dict[str, Any], now: float) -> bool:
    """Whether a recorded attempt at a charter revision still leaves it owed.

    A fetched charter is done. One the server said is absent waits out
    `_RETRY_AFTER`, as does a record from before attempts were counted. One
    that failed transiently stays owed until it has failed `_MAX_TRIES` times,
    then waits out `_TRANSIENT_LOCKOUT`.
    """
    if have.get("ok"):
        return False
    age = float(now - have.get("at", 0))
    if have.get("absent") or "tries" not in have:
        return age >= _RETRY_AFTER
    if have["tries"] >= _MAX_TRIES:
        return age >= _TRANSIENT_LOCKOUT
    return True


def _pending_charters(
    groups: List[Dict[str, Any]], fetched: Dict[str, Dict[str, Any]], now: float
) -> List[Dict[str, Any]]:
    """Groups whose charter revision is not yet held, active ones first."""
    pending: List[Dict[str, Any]] = []
    for group in groups:
        if not group.get("charter_doc") or not group.get("charter_rev"):
            continue
        have = fetched.get(group["acronym"], {})
        if have.get("rev") == group["charter_rev"] and not _is_pending(have, now):
            continue
        pending.append(group)
    pending.sort(key=lambda g: (reader.is_closed(g), g["acronym"]))
    return pending


# --- Entry point ------------------------------------------------------------


def ensure_groups_index(
    verbosity: Verbosity = Verbosity.STATUS,
    force: bool = False,
    fetch: Optional[Fetcher] = None,
    charter_fetch: Optional[Callable[[str, str], Optional[str]]] = None,
    embed_model: Optional[str] = None,
    embed: bool = True,
    batch: int = CHARTER_BATCH,
    seed: bool = True,
) -> None:
    """Refresh the chartered-group mirror, then its charter index. Never raises.

    On a machine with no mirror yet this first installs the seed store's
    snapshot, so the refresh below is the incremental delta rather than a
    cold build of every feed and charter.

    Not run on the cloud backend: its hosts are ephemeral, so a mirror built
    there would be rebuilt (and the upstreams re-hit) on every gather until the
    mirror has a durable home in the fleet store.
    """
    if service_config.store_backend() == "cloud":
        return
    reindex = False
    try:
        if seed and not os.path.exists(
            os.path.join(reader.groups_files_dir(), reader.GROUPS_FILE)
        ):
            reindex = install_seed(verbosity)
        _ensure(
            verbosity,
            force,
            fetch or _fetch_all,
            charter_fetch or _fetch_charter,
            embed_model,
            embed,
            batch,
            reindex,
        )
    except Exception as err:  # pylint: disable=broad-except
        log(f"Groups: refresh failed: {err}", verbosity, LogLevel.PROGRESS)


def _ensure(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    verbosity: Verbosity,
    force: bool,
    fetch: Fetcher,
    charter_fetch: Callable[[str, str], Optional[str]],
    embed_model: Optional[str],
    embed: bool,
    batch: int,
    reindex: bool,
) -> None:
    files = reader.groups_files_dir()
    state = reader.state_dir()
    try:
        os.makedirs(reader.charters_dir(), exist_ok=True)
        os.makedirs(state, exist_ok=True)
    except OSError as err:
        log(f"Groups: cannot create {files}: {err}", verbosity, LogLevel.PROGRESS)
        return
    groups_path = os.path.join(files, reader.GROUPS_FILE)
    fetched_path = os.path.join(state, "charters.json")
    fetched: Dict[str, Dict[str, Any]] = _read_json(fetched_path, {})
    groups: List[Dict[str, Any]] = _read_json(groups_path, [])
    failed_stamp = os.path.join(state, "last-failed-refresh")
    stale = force or not _mirror.is_fresh(groups_path, reader.GROUPS_TTL_SECONDS)
    # A failed refresh leaves the TTL expired, so without this every run would
    # repeat the bulk requests against a Datatracker that is already struggling.
    if stale and not force and _mirror.is_fresh(failed_stamp, _FAILURE_BACKOFF):
        stale = False
    # A stamp of the last charter batch, so a backlog drains across runs
    # without every invocation inside the TTL fetching.
    batch_stamp = os.path.join(state, "last-charter-batch")
    backlog = bool(_pending_charters(groups, fetched, time.time()))
    batch_due = backlog and not _mirror.is_fresh(batch_stamp, _BATCH_INTERVAL)
    pending_stamp = os.path.join(state, "index-pending")
    if not stale and not batch_due:
        if reindex:
            _touch(pending_stamp)
        _refresh_index(pending_stamp, verbosity, embed_model, embed)
        return

    changed = False
    refreshed = False
    if stale:
        refreshed, changed = _refresh_derived(
            files, state, groups_path, fetch, verbosity
        )
        groups = _read_json(groups_path, groups)
        if refreshed:
            _mirror.unlink(failed_stamp)
        else:
            _touch(failed_stamp)
    texts: Dict[str, str] = {}
    if batch_due or stale:
        texts = _fetch_charter_batch(groups, fetched, charter_fetch, batch, verbosity)
        _touch(batch_stamp)
    dirty = changed or bool(texts)
    if dirty or refreshed:
        _write_charter_files(groups, texts)
    _write_json(fetched_path, fetched, verbosity)
    if refreshed:
        _mirror.touch(groups_path)
        record_gather(reader.GROUPS_CORPUS)
    if dirty or reindex:
        _touch(pending_stamp)
    _refresh_index(pending_stamp, verbosity, embed_model, embed)


def install_seed(verbosity: Verbosity = Verbosity.STATUS) -> bool:
    """Install the seed store's `_groups` snapshot. Best-effort; returns
    whether one was installed.

    Only ever a cold start: once a mirror exists it refreshes itself
    incrementally, which is cheaper than another download. The snapshot is
    taken whole, state files and embedding index included; if its embedding
    model is not this machine's the index builder re-embeds, which for a few
    thousand short chunks is the cost of using a different model, not a failure.
    """
    if not service_config.seeding_enabled():
        return False
    # Lazy: the seed consumer is gather-path only, and this keeps it off any
    # path that merely reads.
    # pylint: disable=import-outside-toplevel
    from ...seed import fetch as seed_fetch
    from ...seed import format as seed_fmt
    from ...seed import generation as seed_generation

    # pylint: enable=import-outside-toplevel
    seed_url = seed_generation.store_url()
    if not seed_url:
        return False
    index = seed_fetch.load_index(seed_url)
    entry = index.entry(reader.GROUPS_CORPUS) if index else None
    if entry is None:
        return False
    try:
        seed_fetch.install(seed_url, entry)
    except (seed_fetch.SeedFetchError, seed_fmt.SeedFormatError) as err:
        log(f"Groups: seed failed ({err})", verbosity, LogLevel.PROGRESS)
        return False
    log(
        f"Groups: seeded from {seed_url} ({entry.gathered})", verbosity, LogLevel.STATUS
    )
    return True


def _touch(path: str) -> None:
    try:
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except OSError:
        pass


def _refresh_derived(
    files: str, state: str, groups_path: str, fetch: Fetcher, verbosity: Verbosity
) -> Tuple[bool, bool]:
    """Fetch every feed and rewrite groups/milestones/timeline JSON. Returns
    (refreshed, changed): whether the group list was obtained at all, and
    whether it or any feed produced something new."""
    group_rows = fetch(f"{_API}/group/group/?type__in={_GROUP_TYPES}", verbosity)
    area_rows = fetch(f"{_API}/group/group/?type__in=area,irtf", verbosity)
    charter_rows = fetch(f"{_API}/doc/document/?type=charter", verbosity)
    # An empty list is as unusable as a failed fetch, and worse: it would be
    # written over the mirror and every charter file deleted with it.
    if not group_rows or not area_rows or not charter_rows:
        log(
            "Groups: group list unavailable; keeping existing mirror",
            verbosity,
            LogLevel.PROGRESS,
        )
        return False, False
    marks_path = os.path.join(state, "watermarks.json")
    marks: Dict[str, str] = _read_json(marks_path, {})
    feeds: Dict[str, Dict[str, Dict[str, Any]]] = {}
    changed = False
    for feed in _FEEDS:
        rows, did = _refresh_feed(
            feed, os.path.join(state, f"{feed}.json"), marks, fetch, verbosity
        )
        feeds[feed] = rows
        if did:
            _write_json(os.path.join(state, f"{feed}.json"), rows, verbosity)
            changed = True
    _write_json(marks_path, marks, verbosity)
    groups = build_groups(group_rows, area_rows, charter_rows)
    by_uri = {g["uri"]: g["acronym"] for g in groups if g["uri"]}
    changed = _write_json(groups_path, groups, verbosity) or changed
    _write_json(
        os.path.join(files, reader.MILESTONES_FILE),
        build_milestones(feeds["milestones"], by_uri),
        verbosity,
    )
    _write_json(
        os.path.join(files, reader.TIMELINE_FILE),
        build_timeline(feeds, by_uri, _rfc_lookup()),
        verbosity,
    )
    log(f"Groups: {len(groups)} groups", verbosity, LogLevel.PROGRESS)
    return True, changed


def _fetch_charter_batch(
    groups: List[Dict[str, Any]],
    fetched: Dict[str, Dict[str, Any]],
    charter_fetch: Callable[[str, str], Optional[str]],
    batch: int,
    verbosity: Verbosity,
) -> Dict[str, str]:
    """Fetch up to `batch` pending charters. Returns the texts obtained and
    records the outcome of each in `fetched`; the caller persists both, files
    first, so a record never claims text that was not written.

    A definite answer (the text, or "no such revision") is recorded as such. A
    transient failure is only counted against the charter, which stays pending
    until it has failed `_MAX_TRIES` times, and several in a row end this
    batch. Recording them as final would block a charter for the retry window
    over a network blip, and not counting them would let one that always
    fails sit at the front of the queue forever.
    """
    now = time.time()
    pending = _pending_charters(groups, fetched, now)[: max(0, batch)]
    texts: Dict[str, str] = {}
    missing = transient = streak = 0
    for group in pending:
        acronym, rev = group["acronym"], group["charter_rev"]
        text = charter_fetch(group["charter_doc"], rev)
        if text is None:
            transient += 1
            streak += 1
            have = fetched.get(acronym, {})
            tries = have.get("tries", 0) if have.get("rev") == rev else 0
            fetched[acronym] = {"rev": rev, "ok": False, "at": now, "tries": tries + 1}
            if streak >= _MAX_CONSECUTIVE_FAILURES:
                break
            continue
        streak = 0
        fetched[acronym] = {"rev": rev, "ok": bool(text), "at": now}
        if text:
            texts[acronym] = text
        else:
            fetched[acronym]["absent"] = True
            missing += 1
    if pending:
        log(
            f"Groups: {len(texts)}/{len(pending)} charters fetched"
            f" ({missing} absent, {transient} failed)"
            + (", stopping early" if streak >= _MAX_CONSECUTIVE_FAILURES else ""),
            verbosity,
            LogLevel.PROGRESS,
        )
    return texts


def _write_charter_files(
    groups: Iterable[Dict[str, Any]], texts: Dict[str, str]
) -> None:
    """Render every group's charter file. Text comes from this run's fetches,
    else from the file already on disk, so a milestone-only change re-renders
    without refetching."""
    milestones = _read_json(
        os.path.join(reader.groups_files_dir(), reader.MILESTONES_FILE), {}
    )
    keep = set()
    for group in groups:
        acronym = group["acronym"]
        keep.add(f"{acronym}.md")
        text = texts.get(acronym) or reader.charter_text(acronym)
        content = render_charter_file(group, text, milestones.get(acronym, []))
        write_if_changed(reader.charter_file(acronym), content)
    if not keep:
        return
    try:
        for name in os.listdir(reader.charters_dir()):
            if name.endswith(".md") and name not in keep:
                os.remove(os.path.join(reader.charters_dir(), name))
    except OSError:
        pass


def _index_built() -> bool:
    return os.path.isfile(
        os.path.join(get_index_dir(), reader.GROUPS_CORPUS, "embeddings.db")
    )


def _refresh_index(
    pending_stamp: str, verbosity: Verbosity, embed_model: Optional[str], embed: bool
) -> None:
    """Build the charter index if a file it embeds may have changed.

    Loading the embedding model is the expensive part, so this runs only when
    the stamp says an earlier pass is owed (a file changed, a snapshot was
    installed, or that pass failed) or no index exists. The stamp is cleared
    only by a pass that completed, so a machine without the embedding library
    keeps trying rather than quietly serving an index that is behind its files.
    """
    if not (os.path.exists(pending_stamp) or not _index_built()):
        return
    if _index(verbosity, embed_model, embed):
        _mirror.unlink(pending_stamp)


def _index(verbosity: Verbosity, embed_model: Optional[str], embed: bool) -> bool:
    """Embed the rendered charters; True if the index is now current. Needs the
    embedding stack, so it is imported here and any failure is the index's
    alone. Incremental on file content, so a pass over unchanged files is cheap."""
    if not embed or not os.path.isdir(reader.charters_dir()):
        return False
    # pylint: disable-next=import-outside-toplevel
    from ...embeddings import DEFAULT_EMBED_MODEL, _get_embed_model, build_index

    model_name = embed_model or DEFAULT_EMBED_MODEL
    # build_index reports an unavailable model as "nothing changed", which is
    # indistinguishable from success, so ask first.
    if _get_embed_model(model_name, verbosity) is None:
        return False
    try:
        build_index(
            reader.GROUPS_CORPUS,
            reader.groups_files_dir(),
            model_name=model_name,
            verbose=verbosity,
        )
    except Exception as err:  # pylint: disable=broad-except
        log(f"Groups: charter index not built: {err}", verbosity, LogLevel.PROGRESS)
        return False
    return True
