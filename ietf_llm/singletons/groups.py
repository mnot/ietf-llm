"""Reader for the chartered-group mirror: every WG and RG, active or concluded.

A cross-corpus singleton, not a gathered Working Group. It lives at
`~/.cache/ietf-llm/_groups/` (the leading underscore keeps it out of
`list_corpora` / `ietf-llm --list`) and is laid out like a corpus so the
embedding and seed machinery apply unchanged:

  _groups/files/groups.json            one record per group
  _groups/files/milestones.json        {acronym: [milestone, ...]}
  _groups/files/timeline.json          {acronym: [event, ...]}, oldest first
  _groups/files/charters/<acronym>.md  charter text + milestones, what is embedded
  _groups/files/state/                 writer-side watermarks and raw rows

`gather.sources.groups.ensure_groups_index` is the only writer. This module is
read-only and never touches the network, same boundary as the rest of the MCP
server; semantic search over the charters lives in `mcp.groups`, beside the
embedding code, and everything here degrades to keyword matching without it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from ..config import service as service_config
from ..paths import get_cache_dir

#: Cache subdirectory, and the corpus name the charter index is built under.
GROUPS_DIR = "_groups"
GROUPS_CORPUS = "_groups"

GROUPS_FILE = "groups.json"
MILESTONES_FILE = "milestones.json"
TIMELINE_FILE = "timeline.json"
CHARTERS_SUBDIR = "charters"
STATE_SUBDIR = "state"

#: Groups change slowly; a day is plenty.
GROUPS_TTL_SECONDS = 24 * 60 * 60

#: Marks the charter body inside a rendered `charters/<acronym>.md`.
CHARTER_HEADING = "## Charter"
MILESTONES_HEADING = "## Milestones"

#: Datatracker group states that mean the group is no longer doing work.
CLOSED_STATES = frozenset({"conclude", "concluded", "bof-conc", "abandon", "replaced"})

_TYPE_LABELS = {"wg": "Working Group", "rg": "Research Group"}

_STATE_LABELS = {
    "active": "active",
    "bof": "BoF — pre-WG, not chartered",
    "bof-conc": "BoF (concluded)",
    "conclude": "concluded",
    "concluded": "concluded",
    "dormant": "dormant",
    "proposed": "proposed",
    "abandon": "abandoned",
    "replaced": "replaced",
}

NOT_GATHERED = (
    "The chartered-group mirror has not been gathered yet. It populates "
    "automatically the next time you run `ietf-llm <corpus>` (any corpus), or "
    "with `ietf-llm --init`; run one and retry."
)

_CLEAN_RE = re.compile(r"""[\]().,?:;"'/]""")


def not_gathered() -> str:
    """The not-gathered message, honest about the cloud backend, where the
    mirror is not refreshed at all."""
    if service_config.store_backend() == "cloud":
        return (
            "The chartered-group mirror is not available on the cloud backend "
            "yet. `find_efforts` still ranks the active efforts."
        )
    return NOT_GATHERED


def groups_dir() -> str:
    """Path to the mirror (not created here: readers never materialise cache)."""
    return os.path.join(get_cache_dir(), GROUPS_DIR)


def groups_files_dir() -> str:
    return os.path.join(groups_dir(), "files")


def charters_dir() -> str:
    return os.path.join(groups_files_dir(), CHARTERS_SUBDIR)


def charter_file(acronym: str) -> str:
    return os.path.join(charters_dir(), f"{acronym}.md")


def state_dir() -> str:
    return os.path.join(groups_files_dir(), STATE_SUBDIR)


def _path(name: str) -> str:
    return os.path.join(groups_files_dir(), name)


# --- Memoised loads (mtime-keyed so a refresh is picked up live) -----------

_CACHE: Dict[str, Tuple[float, Any]] = {}


def _load(name: str) -> Any:
    path = _path(name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _CACHE.pop(path, None)
        return None
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    _CACHE[path] = (mtime, loaded)
    return loaded


def load_groups() -> Optional[List[Dict[str, Any]]]:
    """Every group record, or None if the mirror has not been gathered."""
    loaded = _load(GROUPS_FILE)
    return loaded if isinstance(loaded, list) else None


def load_milestones() -> Dict[str, List[Dict[str, Any]]]:
    loaded = _load(MILESTONES_FILE)
    return loaded if isinstance(loaded, dict) else {}


def load_timeline() -> Dict[str, List[Dict[str, Any]]]:
    loaded = _load(TIMELINE_FILE)
    return loaded if isinstance(loaded, dict) else {}


def get_group(acronym: str) -> Optional[Dict[str, Any]]:
    for group in load_groups() or []:
        if group.get("acronym") == acronym:
            return group
    return None


def is_closed(group: Dict[str, Any]) -> bool:
    return str(group.get("state") or "").lower() in CLOSED_STATES


def state_label(group: Dict[str, Any]) -> str:
    state = str(group.get("state") or "").lower()
    return _STATE_LABELS.get(state, state)


def type_label(group: Dict[str, Any]) -> str:
    kind = str(group.get("type") or "").lower()
    return _TYPE_LABELS.get(kind, kind)


def charter_text(acronym: str) -> Optional[str]:
    """The charter body from the rendered file, or None when the group has
    no charter text (the file then carries only the Datatracker description)."""
    try:
        with open(charter_file(acronym), "r", encoding="utf-8") as handle:
            body = handle.read()
    except OSError:
        return None
    if CHARTER_HEADING not in body:
        return None
    text = body.split(CHARTER_HEADING, 1)[1]
    text = text.split(MILESTONES_HEADING, 1)[0]
    return text.strip() or None


def _clean(value: str) -> str:
    return _CLEAN_RE.sub("", str(value).lower())


def _is_cached(acronym: str) -> bool:
    return os.path.isdir(os.path.join(get_cache_dir(), acronym, "files"))


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# --- Milestones -------------------------------------------------------------


def milestone_status(milestone: Dict[str, Any], today: Optional[date] = None) -> str:
    """One word for where a milestone stands: done, dropped, overdue, ..."""
    state = str(milestone.get("state") or "").lower()
    if state == "deleted":
        return "dropped"
    resolved = str(milestone.get("resolved") or "").strip()
    if resolved:
        return resolved.lower()
    if state == "review":
        return "needs review"
    if state == "charter":
        return "proposed in recharter"
    due = _parse_date(milestone.get("due"))
    if due is not None and due < (today or date.today()):
        return "overdue"
    return "open"


#: Statuses that mean the work is still ahead of the group. "Needs review" is
#: included: the milestone is live, only its change awaits approval.
OPEN_STATUSES = frozenset({"open", "overdue", "needs review"})

#: Neither open nor done: proposed by a recharter that has not been approved.
_PROPOSED = "proposed in recharter"


def is_open(milestone: Dict[str, Any]) -> bool:
    return milestone_status(milestone) in OPEN_STATUSES


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def milestone_line(milestone: Dict[str, Any], today: Optional[date] = None) -> str:
    due = str(milestone.get("due") or "")[:7] or "no date"
    docs = milestone.get("docs") or []
    suffix = f" ({', '.join(docs)})" if docs else ""
    desc = str(milestone.get("desc", "")).strip()
    return f"[{due}] {desc}{suffix} — {milestone_status(milestone, today)}"


def render_milestones(
    *,
    group: Optional[str] = None,
    area: Optional[str] = None,
    status: str = "open",
    due_before: Optional[str] = None,
    due_after: Optional[str] = None,
    include_closed_groups: bool = False,
    limit: int = 50,
) -> str:
    """Milestones across groups, soonest due first."""
    groups = load_groups()
    if groups is None:
        return not_gathered()
    wanted = status.lower()
    if wanted not in {"open", "overdue", "done", "all"}:
        return "`status` must be one of: open, overdue, done, all."
    for label, value in (("due_before", due_before), ("due_after", due_after)):
        if value and _parse_date(value) is None:
            return f"`{label}` must be an ISO date (YYYY-MM-DD), got {value!r}."
    by_acronym = {g.get("acronym"): g for g in groups}
    if group and group not in by_acronym:
        return f"No chartered group `{group}` in the mirror."
    rows: List[Tuple[str, str, Dict[str, Any]]] = []
    for acronym, items in load_milestones().items():
        info = by_acronym.get(acronym)
        if info is None or (group and acronym != group):
            continue
        if area and area.lower() not in {
            str(info.get("area", "")).lower(),
            str(info.get("area_name", "")).lower(),
        }:
            continue
        if is_closed(info) and not (include_closed_groups or group):
            continue
        for item in items:
            current = milestone_status(item)
            if current == "dropped":
                continue
            if wanted == "open" and current not in OPEN_STATUSES:
                continue
            if wanted == "overdue" and current != "overdue":
                continue
            if wanted == "done" and (current in OPEN_STATUSES or current == _PROPOSED):
                continue
            due = str(item.get("due") or "")
            if due_before and (not due or due > due_before):
                continue
            if due_after and (not due or due < due_after):
                continue
            rows.append((due or "9999", acronym, item))
    rows.sort(key=lambda r: (r[0], r[1]))
    if not rows:
        return "No milestones match."
    shown = rows[: max(0, limit)]
    lines = [f"**{len(rows)} milestone{'s' if len(rows) != 1 else ''}**"]
    if len(shown) < len(rows):
        lines[0] += f" (showing {len(shown)})"
    lines.append("")
    for _due, acronym, item in shown:
        lines.append(f"- **{acronym}** {milestone_line(item)}")
    lines += [
        "",
        "_Milestone dates are the group's own targets as recorded in the "
        "Datatracker; many are stale. `overdue` means open and past due._",
    ]
    return "\n".join(lines)


# --- Timeline ---------------------------------------------------------------


def render_timeline(acronym: str, year: Optional[int] = None) -> str:
    groups = load_groups()
    if groups is None:
        return not_gathered()
    if get_group(acronym) is None:
        return f"No chartered group `{acronym}` in the mirror."
    events = load_timeline().get(acronym) or []
    if year is not None:
        events = [e for e in events if str(e.get("t", "")).startswith(f"{year}-")]
    if not events:
        return f"No timeline events recorded for `{acronym}`" + (
            f" in {year}." if year is not None else "."
        )
    lines = [f"# {acronym} — timeline", ""]
    current_year = ""
    for event in sorted(events, key=lambda e: str(e.get("t", "")), reverse=True):
        when = str(event.get("t", ""))
        if when[:4] != current_year:
            current_year = when[:4]
            lines += [f"## {current_year}", ""]
        lines.append(f"- {when} · {event.get('title', '')}")
    lines += [
        "",
        "_From Datatracker group, charter and milestone records and the RFC "
        "series, plus chair changes recorded since the Datatracker event log began. "
        "It does not cover draft adoption or meetings; "
        "a gathered corpus's own timeline (`read_digest`) has those._",
    ]
    return "\n".join(lines)


# --- Group info -------------------------------------------------------------


def _lifecycle(acronym: str, closed: bool) -> str:
    """First charter approval and, for a closed group, its last state change."""
    events = load_timeline().get(acronym) or []
    bits = []
    approvals = [e for e in events if e.get("kind") == "charter-approved"]
    if approvals:
        bits.append(f"first charter approval {approvals[0]['t']}")
    states = [e for e in events if e.get("kind") == "group-state"]
    if closed and states:
        bits.append(f"last state change {states[-1]['t']} ({states[-1]['title']})")
    return "; ".join(bits)


def render_group(acronym: str, include_charter: bool = True) -> str:
    """Everything the mirror holds about one group."""
    groups = load_groups()
    if groups is None:
        return not_gathered()
    group = get_group(acronym)
    if group is None:
        return (
            f"No chartered group `{acronym}` in the mirror. It covers every "
            "Working Group and Research Group, active or concluded; try "
            "`search_groups` for a topic."
        )
    heading = f"# {acronym} — {group.get('name') or '(unnamed)'}"
    lines = [heading, ""]
    facets = [type_label(group), state_label(group)]
    area = group.get("area_name") or group.get("area")
    if area:
        facets.append(f"{area} ({group['area']})" if group.get("area") else area)
    lines.append(" · ".join(f for f in facets if f))
    if group.get("charter_rev"):
        draft = group.get("charter_draft_rev")
        note = {
            "rechartering": f"; rechartering in progress (working revision {draft})",
            "unapproved": "; never approved",
        }.get(str(group.get("charter_status") or ""), "")
        lines.append(
            f"**Charter:** rev {group['charter_rev']}{note} — "
            f"https://datatracker.ietf.org/doc/{group.get('charter_doc')}/"
        )
    if group.get("list_email"):
        lines.append(f"**Mailing list:** {group['list_email']}")
    span = _lifecycle(acronym, is_closed(group))
    if span:
        lines.append(f"**Lifecycle:** {span}")
    lines.append(
        "**Gathered here:** "
        + ("✓ yes — query it with the corpus tools" if _is_cached(acronym) else "no")
    )
    if is_closed(group):
        lines.append(
            "\n_This group is no longer active; its charter is the last one it "
            "had, not a description of current work._"
        )
    if include_charter:
        text = charter_text(acronym)
        lines += ["", "## Charter", ""]
        if text:
            lines.append(text)
        else:
            lines.append(
                "(No charter text available; the Datatracker description follows.)"
            )
            lines += ["", str(group.get("description") or "").strip()]
    items = [
        m
        for m in load_milestones().get(acronym, [])
        if milestone_status(m) != "dropped"
    ]
    if items:
        open_items = [m for m in items if is_open(m)]
        lines += ["", f"## Milestones ({len(open_items)} open of {len(items)})", ""]
        lines += [f"- {milestone_line(m)}" for m in sorted(items, key=_milestone_key)]
    recent = (load_timeline().get(acronym) or [])[-6:]
    if recent:
        lines += ["", "## Recent timeline", ""]
        lines += [f"- {e['t']} · {e.get('title', '')}" for e in reversed(recent)]
        lines.append(f"\n`group_timeline({acronym!r})` has the full history.")
    return "\n".join(lines)


def _milestone_key(milestone: Dict[str, Any]) -> Tuple[int, str]:
    return (0 if is_open(milestone) else 1, str(milestone.get("due") or "9999"))


# --- Keyword search (fallback when the charter index is not installed) ------


def keyword_search(
    query: str,
    *,
    state: Optional[str] = None,
    area: Optional[str] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Groups ranked by keyword overlap with name and description."""
    terms = [t for t in _clean(query).split() if len(t) >= 3]
    scored: List[Tuple[int, str, Dict[str, Any]]] = []
    for group in load_groups() or []:
        if not passes_filters(group, state=state, area=area, kind=kind):
            continue
        name = _clean(group.get("name", ""))
        desc = _clean(group.get("description", ""))
        acronym = _clean(group.get("acronym", ""))
        score = 0
        for term in terms:
            if term == acronym:
                score += 100
            score += 10 * name.count(term) + desc.count(term)
        if score:
            scored.append((score, group.get("acronym", ""), group))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [group for _, _, group in scored]


def passes_filters(
    group: Dict[str, Any],
    *,
    state: Optional[str] = None,
    area: Optional[str] = None,
    kind: Optional[str] = None,
) -> bool:
    if state:
        wanted = state.lower()
        have = str(group.get("state") or "").lower()
        if wanted == "closed":
            if not is_closed(group):
                return False
        elif wanted != have:
            return False
    if area and area.lower() not in {
        str(group.get("area", "")).lower(),
        str(group.get("area_name", "")).lower(),
    }:
        return False
    if kind and kind.lower() != str(group.get("type") or "").lower():
        return False
    return True


def summary(group: Dict[str, Any], limit: int = 200) -> str:
    """One line on what a group does. The Datatracker description is usually
    the charter's opening paragraph but is sometimes a stub or a heading, so
    a short one is replaced by the charter's first substantial paragraph."""
    desc = " ".join(str(group.get("description") or "").split())
    if len(desc) < 60:
        for para in (charter_text(group.get("acronym", "")) or "").split("\n\n"):
            para = " ".join(para.split())
            if len(para) >= 60:
                desc = para
                break
    return _truncate(desc, limit)


def render_group_rows(
    groups: List[Dict[str, Any]], scores: Dict[str, float]
) -> List[str]:
    """Result rows shared by `search_groups`'s semantic and keyword paths."""
    lines: List[str] = []
    for group in groups:
        acronym = group.get("acronym", "")
        bits = [type_label(group), state_label(group), group.get("area")]
        score = f" · score {scores[acronym]:.3f}" if acronym in scores else ""
        cached = " · ✓ gathered" if _is_cached(acronym) else ""
        lines.append(
            f"- **{acronym}** — {group.get('name', '(unnamed)')} · "
            f"{' · '.join(str(b) for b in bits if b)}{score}{cached}"
        )
        desc = summary(group)
        if desc:
            lines.append(f"  {desc}")
    return lines
