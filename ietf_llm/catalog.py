"""Reader for the active-effort catalog mirrored from Datatracker.

A cross-corpus singleton, not a gathered Working Group: the catalog
spans every active IETF/IRTF effort, so it lives at
`~/.cache/ietf-llm/_catalog/` (leading underscore keeps it out of
`list_corpora` / `ietf-llm --list`, which enumerate real corpora)
rather than under a corpus name.

The data is one derived JSON blob — `_catalog/catalog.json`, a slim list
of effort records — synthesised from the Datatracker group collection by
`gather.catalog.ensure_catalog_index` (the only writer):

  [{acronym, name, type, state, area, area_name, description}, ...]

This module is read-only and never touches the network — same boundary
as the rest of the MCP server. It answers the topic-first question
("what is the IETF doing around X?") that the corpus-first tools can't:
rank active efforts by a free-text topic and tag each with whether it is
already gathered here, so the model prefers an already-cached corpus
over a fresh gather. We render markdown rather than JSON to match every
other ietf-llm tool and to keep result lists token-cheap.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .corpus import kind_status
from .utils import get_cache_dir

#: Cache subdirectory for the effort-catalog singleton.
CATALOG_DIR = "_catalog"

#: The derived, reader-facing blob (the writer also keeps raw source
#: mirrors beside it, which this module ignores).
CATALOG_FILE = "catalog.json"

#: A description/name term must be at least this long to match, so a
#: stray "ai" inside "email" / "domain" doesn't flood the ranking. An
#: acronym match is exempt (an effort *is* its short acronym).
PREFIX_LEN = 3

#: Relative weights: an acronym is the strongest topic signal, a name
#: word next, a description word weakest (and capped per term so a long
#: charter abstract can't out-shout a focused name match).
_W_ACRONYM_EXACT = 100
_W_ACRONYM_PREFIX = 40
_W_NAME = 10
_W_DESC = 1

_CLEAN_RE = re.compile(r"""[\]().,?:;"'/]""")


def catalog_index_dir() -> str:
    """Path to the catalog cache dir (not created here — readers never
    materialise cache; the gatherer owns writes)."""
    return os.path.join(get_cache_dir(), CATALOG_DIR)


def _catalog_file() -> str:
    return os.path.join(catalog_index_dir(), CATALOG_FILE)


def _clean(value: str) -> str:
    """Lowercase and strip punctuation, matching the RFC reader's
    `clean_string` so both indexes tokenize a query the same way."""
    return _CLEAN_RE.sub("", str(value).lower())


def _terms(query: str) -> List[str]:
    return [t for t in _clean(query).split() if t]


# --- Memoised load (mtime-keyed so a re-gather is picked up live) ----------

_CACHE: Optional[Tuple[float, List[Dict[str, Any]]]] = None


def _load() -> Optional[List[Dict[str, Any]]]:
    """Return the parsed catalog, or None if it hasn't been gathered.

    Memoised on `catalog.json`'s mtime so a long-running MCP server picks
    up a re-gather without a restart, while warm calls skip the parse.
    """
    global _CACHE  # pylint: disable=global-statement
    path = _catalog_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _CACHE = None
        return None
    if _CACHE is not None and _CACHE[0] == mtime:
        return _CACHE[1]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return None
    efforts = loaded if isinstance(loaded, list) else []
    _CACHE = (mtime, efforts)
    return efforts


_NOT_GATHERED = (
    "The effort catalog has not been gathered yet. It populates "
    "automatically the next time you run `ietf-llm <corpus>` (any "
    "corpus); run one and retry. Meanwhile `list_corpora` shows what is "
    "already cached here."
)


# --- Ranking ---------------------------------------------------------------


def _word_prefix_hit(text: str, term: str) -> bool:
    """True if any whitespace-split word of `text` starts with `term`."""
    for word in str(text).split():
        if _clean(word).startswith(term):
            return True
    return False


def _score_term(effort: Dict[str, Any], term: str) -> int:
    """Topic relevance of one query term to one effort. Acronym beats
    name beats description; a sub-`PREFIX_LEN` term only ever matches a
    full/prefix acronym (where short tokens like `tls` are meaningful)."""
    acronym = _clean(effort.get("acronym", ""))
    if acronym == term:
        return _W_ACRONYM_EXACT
    if acronym.startswith(term):
        return _W_ACRONYM_PREFIX
    if len(term) < PREFIX_LEN:
        return 0
    score = 0
    if _word_prefix_hit(effort.get("name", ""), term):
        score += _W_NAME
    if _word_prefix_hit(effort.get("description", ""), term):
        score += _W_DESC
    return score


def _rank(efforts: List[Dict[str, Any]], terms: List[str]) -> List[Dict[str, Any]]:
    """Efforts with any term match, best first; ties broken by acronym so
    the order is stable across runs."""
    scored: List[Tuple[int, str, Dict[str, Any]]] = []
    for effort in efforts:
        total = sum(_score_term(effort, term) for term in terms)
        if total > 0:
            scored.append((total, effort.get("acronym", ""), effort))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [effort for _, _, effort in scored]


# --- Cached-tag + rendering ------------------------------------------------


def _is_cached(acronym: str) -> bool:
    """True if this effort has been gathered locally. Read-only existence
    check (mirrors the MCP server's `_corpus_exists`) — never creates a
    dir, so the tag can't materialise a junk cache."""
    return os.path.isdir(os.path.join(get_cache_dir(), acronym, "files"))


def _cached_tag(acronym: str) -> str:
    """`✓ cached (kind · status)` when gathered, else a gather hint."""
    if not _is_cached(acronym):
        return "not gathered"
    kind, status = kind_status(acronym)
    detail = f"{kind} · {status}" if status else kind
    return f"✓ cached ({detail})"


def _facets(effort: Dict[str, Any]) -> str:
    bits = [effort.get("type"), effort.get("area")]
    return " · ".join(b for b in bits if b)


def _is_bof(effort: Dict[str, Any]) -> bool:
    """True for a pre-WG BoF — the load-bearing distinction the row must
    not blur into 'active Working Group'."""
    return str(effort.get("state") or "").strip().lower() == "bof"


def _state_label(effort: Dict[str, Any]) -> str:
    """Render the catalog's group `state` for a row, self-documenting so a
    reader never has to infer status from a bare row.

    The catalog mirrors the `active` and `bof` Datatracker slices, so a row
    is one or the other. A **BoF is a pre-WG effort** — no chartered group
    exists yet to receive a proposal — and that distinction is load-bearing,
    so it's spelled out inline rather than left as a three-letter tag. An
    empty state (shouldn't occur for catalog efforts) renders nothing rather
    than a misleading marker.
    """
    if _is_bof(effort):
        return "**BoF — pre-WG, not chartered**"
    return str(effort.get("state") or "").strip().lower()


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def render_efforts(query: str, limit: int = 15) -> str:
    """Rank active IETF/IRTF efforts by a free-text topic; return a
    compact markdown list, each tagged with whether it is cached here."""
    catalog = _load()
    if catalog is None:
        return _NOT_GATHERED
    terms = _terms(query)
    if not terms:
        return 'Give a topic to search for, e.g. `find_efforts("congestion control")`.'
    ranked = _rank(catalog, terms)
    total = len(ranked)
    shown = ranked[: max(0, limit)]
    if not shown:
        return (
            f"No active efforts match `{query}`. The catalog covers active "
            "working and research groups only — a concluded effort or one "
            "framed differently may not surface; try `rfc_search` for "
            "published work, or broaden the topic."
        )
    header = f"**{total} effort{'s' if total != 1 else ''}** for `{query}`"
    if len(shown) < total:
        header += f" (showing {len(shown)})"
    header += " — ✓ = already gathered here, prefer those."
    lines = [header, ""]
    any_uncached = False
    any_bof = False
    for effort in shown:
        acronym = effort.get("acronym", "")
        facets = _facets(effort)
        state = _state_label(effort)
        if _is_bof(effort):
            any_bof = True
        bits = " · ".join(b for b in (facets, state) if b)
        suffix = f" · {bits}" if bits else ""
        tag = _cached_tag(acronym)
        if not _is_cached(acronym):
            any_uncached = True
        lines.append(
            f"- **{acronym}** — {effort.get('name', '(unnamed)')}{suffix} · {tag}"
        )
        desc = _truncate(effort.get("description", ""))
        if desc:
            lines.append(f"  {desc}")
    if any_bof:
        lines += [
            "",
            "A **BoF** is a pre-WG effort: there is no chartered group yet to "
            "receive a proposal. New proposals go to the area's dispatch venue "
            "(DISPATCH for ART / WIT / SEC, GENDISPATCH for GEN), with the BoF "
            "as a place to build interest — not as a chartered home.",
        ]
    if any_uncached:
        lines += [
            "",
            "Gather the few that dominate the topic with `ietf-llm <acronym>` "
            "(or `start_gather`), then query those — don't gather them all.",
        ]
    return "\n".join(lines)
