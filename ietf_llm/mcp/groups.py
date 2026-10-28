"""Chartered-group tools: get_group_info, search_groups, list_milestones,
group_timeline, and the charter-aware find_efforts.

Thin wrappers over `singletons.groups` (the mirror) plus the one thing that
reader keeps out, semantic search over the charter index. All read-only and
offline.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Dict, List, Optional

from ..embeddings.search import index_model
from ..embeddings.search import search as _search
from ..log import LogLevel, log
from ..singletons import groups as mirror
from ..singletons.catalog import render_efforts
from .common import _offload

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer  # pragma: no cover

#: Chunks retrieved before collapsing to one score per group. A charter is one
#: to a few chunks, so this is a few dozen distinct groups.
_RETRIEVE_CHUNKS = 120

#: Cosine floor below which a charter is not a topical match. A query with no
#: real answer still has nearest neighbours, and charter scores sit in a narrow
#: band, so a spread-based cutoff cannot tell them apart; an absolute floor
#: can. Calibrated for the default embedding model: nonsense queries top out
#: near 0.61 and real topics start near 0.67. Another model shifts the scale,
#: hence the override (as with the corpus router's floor).
_BUILTIN_FLOOR = 0.62


def _env_floor() -> float:
    raw = os.environ.get("IETF_LLM_GROUPS_MIN_SCORE", "").strip()
    try:
        return float(raw) if raw else _BUILTIN_FLOOR
    except ValueError:
        return _BUILTIN_FLOOR


SEMANTIC_FLOOR = _env_floor()

#: For `find_efforts`, keep only hits this close to the best one, so a
#: confident match is not padded with the tail of the band.
_FIND_WINDOW = 0.10

#: Searchable state slugs as the index stores them.
_STATE_SLUGS = {
    "active": "active",
    "bof": "bof",
    "concluded": "conclude",
    "conclude": "conclude",
    "dormant": "dormant",
    "proposed": "proposed",
}


def index_installed() -> bool:
    return index_model(mirror.GROUPS_CORPUS) is not None


def _area_label(area: Optional[str]) -> Optional[str]:
    """`area:<acronym>` for an area given as acronym or full name."""
    if not area:
        return None
    wanted = area.strip().lower()
    for group in mirror.load_groups() or []:
        if wanted in {
            str(group.get("area", "")).lower(),
            str(group.get("area_name", "")).lower(),
        }:
            return f"area:{str(group['area']).lower()}"
    return f"area:{wanted}"


def semantic_ranking(
    query: str,
    *,
    state: Optional[str] = None,
    area: Optional[str] = None,
    kind: Optional[str] = None,
    window: Optional[float] = None,
) -> Dict[str, float]:
    """Best chunk score per group for `query`, best first. Empty when the
    charter index is not installed or nothing clears the floor. `window`
    additionally drops groups scoring that far below the best."""
    if not query.strip() or not index_installed():
        return {}
    labels = [
        lab
        for lab in (_area_label(area), f"type:{kind.lower()}" if kind else None)
        if lab
    ]
    slug = _STATE_SLUGS.get((state or "").lower())
    try:
        hits = _search(
            mirror.GROUPS_CORPUS,
            query,
            k=_RETRIEVE_CHUNKS,
            label=labels[0] if labels else None,
            state=slug,
            diversify=False,
        )
    except Exception as err:  # pylint: disable=broad-except
        # An unreachable embedding backend must not take `find_efforts` down
        # with it: the keyword ranking still answers.
        log(f"Group charter search unavailable: {err}", level=LogLevel.WARN)
        return {}
    scores: Dict[str, float] = {}
    for hit in hits:
        acronym = hit.file.rsplit("/", 1)[-1].removesuffix(".md")
        if hit.score >= SEMANTIC_FLOOR and acronym not in scores:
            scores[acronym] = hit.score
    if window is not None and scores:
        best = next(iter(scores.values()))
        scores = {a: v for a, v in scores.items() if v >= best - window}
    if len(labels) > 1:
        wanted = labels[1]
        types = {
            g["acronym"]: f"type:{g.get('type')}" for g in mirror.load_groups() or []
        }
        scores = {a: s for a, s in scores.items() if types.get(a) == wanted}
    return scores


def tool_search_groups(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    query: str,
    area: Optional[str] = None,
    state: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 15,
) -> str:
    groups = mirror.load_groups()
    if groups is None:
        return mirror.not_gathered()
    by_acronym = {g["acronym"]: g for g in groups}
    scores = semantic_ranking(query, state=state, area=area, kind=kind)
    note = ""
    if scores:
        ranked = [
            by_acronym[a]
            for a in scores
            if a in by_acronym
            and mirror.passes_filters(by_acronym[a], state=state, area=area, kind=kind)
        ]
    else:
        ranked = mirror.keyword_search(query, state=state, area=area, kind=kind)
        note = (
            "_Keyword match over names and descriptions: the charter index is "
            "not installed here, or nothing in it was close._"
            if not index_installed()
            else "_Nothing in the charters was a close semantic match; showing "
            "keyword matches over names and descriptions._"
        )
    if not ranked:
        return f"No chartered groups match `{query}` with those filters."
    shown = ranked[: max(0, limit)]
    lines = [f"**{len(ranked)} group{'s' if len(ranked) != 1 else ''}** for `{query}`"]
    if len(shown) < len(ranked):
        lines[0] += f" (showing {len(shown)})"
    lines.append("")
    lines += mirror.render_group_rows(shown, scores)
    lines.append("")
    if note:
        lines += [note, ""]
    lines.append(
        "_Concluded groups are included and labelled; their charters describe "
        "work that is over. Read one with `get_group_info`._"
    )
    return "\n".join(lines)


def tool_find_efforts(query: str, limit: int = 15) -> str:
    semantic = list(semantic_ranking(query, window=_FIND_WINDOW))
    return render_efforts(query, limit, semantic=semantic)


def register(server: "MCPServer") -> None:
    @server.tool()
    async def get_group_info(acronym: str, include_charter: bool = True) -> str:
        """Everything the IETF/IRTF record holds about **one chartered group**,
        active or concluded: type, state, area, current charter text,
        milestones with their status and due dates, and a recent timeline.

        Reach for this for "what is X chartered to do", "what are X's
        milestones", "is X still active", or to read a concluded group's
        charter. It is the group-side twin of `get_rfc_info`. It reads a
        local mirror of the Datatracker, not a gathered corpus: for the
        group's mail, drafts and meetings, gather it (`start_gather`) or use
        the corpus tools if it is already `✓ gathered`.

        The charter is the **current** one only. A concluded group's charter
        is the last it had. Milestone dates are the group's own targets and
        are often stale; `overdue` means open and past due.

        `include_charter=False` omits the charter body for a compact summary.
        """
        return await _offload(mirror.render_group, acronym, include_charter)

    @server.tool()
    async def search_groups(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        query: str,
        area: Optional[str] = None,
        state: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 15,
    ) -> str:
        """**Semantic search over the charters of every chartered group** —
        active and concluded Working Groups and Research Groups. Finds the
        groups whose charter is about a subject, even when the wording
        differs from the group's name.

        Reach for this for "which groups work on X", "has the IETF ever
        chartered a group for X", or "what did the old X working group do".
        For "what is the IETF doing about X *now*", `find_efforts` is the
        better entry point: it ranks the same groups but favours active ones
        and tells you which are already gathered. This tool is for searching
        the charters themselves, including concluded groups.

        Filters:
          - `area`: an area acronym or name (`wit`, `sec`, "Applications and
            Real-Time"). Research groups have area `irtf`.
          - `state`: `active`, `concluded`, `bof`, `dormant`, `proposed`, or
            `closed` (any group that is over).
          - `kind`: `wg` or `rg`.
          - `limit`: max results (default 15).

        Falls back to keyword matching over names and descriptions, and says
        so, when the charter index is not installed. Follow a hit with
        `get_group_info`.
        """
        return await _offload(tool_search_groups, query, area, state, kind, limit)

    @server.tool()
    async def list_milestones(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        group: Optional[str] = None,
        area: Optional[str] = None,
        status: str = "open",
        due_before: Optional[str] = None,
        due_after: Optional[str] = None,
        include_closed_groups: bool = False,
        limit: int = 50,
    ) -> str:
        """Charter **milestones across groups**, soonest due first — "what is
        due this quarter", "which milestones are overdue", "what is `httpbis`
        committed to".

        Filters:
          - `group`: one group's acronym (also shows milestones of a concluded
            group).
          - `area`: an area acronym or name.
          - `status`: `open` (default; includes needs-review), `overdue`,
            `done`, or `all`.
          - `due_before` / `due_after`: ISO dates (YYYY-MM-DD).
          - `include_closed_groups`: milestones of concluded groups are
            omitted by default.

        These are the dates the group recorded in the Datatracker. They are
        targets, frequently missed and slow to be updated, so `overdue` is
        not evidence a group is stalled. Dropped milestones are never shown.
        """
        return await _offload(
            mirror.render_milestones,
            group=group,
            area=area,
            status=status,
            due_before=due_before,
            due_after=due_after,
            include_closed_groups=include_closed_groups,
            limit=limit,
        )

    @server.tool()
    async def group_timeline(acronym: str, year: Optional[int] = None) -> str:
        """Dated history of **one chartered group**: state changes (chartered,
        concluded), charter revisions and IESG approvals, milestones added,
        moved, completed or dropped, and the RFCs it published. Newest first,
        grouped by year; `year` narrows to one.

        Chair appointments and departures are included, as far back as the
        Datatracker's event log goes (a group's founding chairs have no
        event). It does **not** cover document adoption or meetings.
        For a group you have gathered, `read_digest` has a fuller timeline
        built from its own mail, drafts and meetings.
        """
        return await _offload(mirror.render_timeline, acronym, year)


__all__: List[str] = ["register", "semantic_ranking", "tool_find_efforts"]
