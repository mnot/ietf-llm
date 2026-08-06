"""Reader for the RFC-series index published by rfc.fyi.

A cross-corpus singleton, not a gathered Working Group: the RFC series
spans everything, so it lives at `~/.cache/ietf-llm/_rfc/` (leading
underscore keeps it out of `list_corpora` / `ietf-llm --list`, which
enumerate real corpora) rather than under a corpus name.

The data is three JSON blobs mirrored from rfc.fyi by
`gather.sources.rfcs.ensure_rfc_index` (the only writer):

  _rfc/rfcs.json   per-RFC metadata (title, status, stream, level,
                   keywords, wg, area, obsoletes)
  _rfc/refs.json   normative / informative references between RFCs
  _rfc/tags.json   curated rfc.fyi collections

This module is read-only and never touches the network — same boundary
as the rest of the MCP server. The search / reference semantics are a
clean-room port of rfc.fyi's `mcp/src/rfcdata.js`: prefix-word matching
(terms shorter than three chars never match), an exact-RFC-number
short-circuit, and obsoletes-aware inbound reference counting. We render
markdown rather than JSON to match every other ietf-llm tool and to keep
result lists token-cheap.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from ..paths import get_cache_dir

#: Cache subdirectory for the RFC index singleton.
RFC_DIR = "_rfc"

#: The three artifacts mirrored from rfc.fyi.
RFC_FILES = ("rfcs.json", "refs.json", "tags.json")

#: How long a mirrored copy is considered current. The series changes
#: slowly, so a day is plenty. This lives here, with the reader, because
#: both sides need the same notion of "too old to trust": the writer
#: (`gather.sources.rfcs`) imports it as the refresh TTL, and the reader
#: uses it to tell a confident "no such RFC" from a miss the mirror is
#: merely too old to know about.
RFC_TTL_SECONDS = 24 * 60 * 60

#: A term must be at least this long to match (mirrors rfc.fyi).
PREFIX_LEN = 3

#: rfc.fyi derives these per-RFC facets into pseudo-tags at runtime.
TAG_TYPES = ("collection", "status", "stream", "level", "wg", "area")

_TEXT_BASE = "https://www.rfc-editor.org/rfc"
_INFO_BASE = "https://www.rfc-editor.org/info"

_CLEAN_RE = re.compile(r"""[\]().,?"']""")


def rfc_index_dir() -> str:
    """Path to the RFC index cache dir (not created here — readers never
    materialise cache; the gatherer owns writes)."""
    return os.path.join(get_cache_dir(), RFC_DIR)


def _rfc_file(name: str) -> str:
    return os.path.join(rfc_index_dir(), name)


def clean_string(value: str) -> str:
    """Lowercase and strip punctuation, matching rfc.fyi's cleanString."""
    return _CLEAN_RE.sub("", str(value).lower())


def rfc_num_to_name(num: str) -> str:
    # rfc.fyi keys (and rfc-editor.org URLs) use the bare number, not a
    # zero-padded form: RFC1, RFC100, RFC9110.
    return f"RFC{int(num)}"


def rfc_name_to_num(name: str) -> str:
    return str(int(name[3:]))


class RfcData:
    """In-memory model over the three rfc.fyi JSON blobs.

    A direct port of rfc.fyi's RfcData: derive facet pseudo-tags from
    each record, invert the reference graph, and invert `obsoletes`.
    """

    def __init__(
        self,
        rfcs: Dict[str, Dict[str, Any]],
        refs: Dict[str, Dict[str, Any]],
        tags: Dict[str, Dict[str, Any]],
    ) -> None:
        self.rfcs = rfcs or {}
        self.refs = refs or {}
        self.tags = tags or {}
        self.all_rfcs = sorted(self.rfcs)
        self.in_refs: Dict[str, List[Tuple[bool, str]]] = {}
        self.obsoleted_by: Dict[str, List[str]] = {}
        self._derive_tags()
        self._compute_references()

    def _derive_tags(self) -> None:
        # status/stream/level/wg/area aren't in tags.json (only
        # collections are); derive them from each record.
        for name in self.all_rfcs:
            rfc = self.rfcs[name]
            for tag_type in TAG_TYPES:
                value = rfc.get(tag_type)
                if not value:
                    continue
                bucket = self.tags.setdefault(tag_type, {})
                bucket.setdefault(value, {"rfcs": []})["rfcs"].append(name)

    def _compute_references(self) -> None:
        for name in self.all_rfcs:
            self.in_refs[name] = []
        for num, rfc_refs in self.refs.items():
            citing = rfc_num_to_name(num)
            for ref in rfc_refs.get("normative", []):
                target = rfc_num_to_name(ref)
                if target in self.in_refs:
                    self.in_refs[target].append((True, citing))
            for ref in rfc_refs.get("informative", []):
                target = rfc_num_to_name(ref)
                if target in self.in_refs:
                    self.in_refs[target].append((False, citing))
        for name in self.all_rfcs:
            for old in self.rfcs[name].get("obsoletes", []):
                self.obsoleted_by.setdefault(old, []).append(name)

    def has(self, name: str) -> bool:
        return name in self.rfcs

    def _matches_term(self, rfc: Dict[str, Any], term: str) -> bool:
        # Title split on spaces; keyword phrases matched whole. Terms
        # shorter than PREFIX_LEN never match, as in the website.
        if len(term) < PREFIX_LEN:
            return False
        for word in rfc.get("title", "").split(" "):
            if clean_string(word).startswith(term):
                return True
        for keyword in rfc.get("keywords", []):
            if clean_string(keyword).startswith(term):
                return True
        return False

    def search(self, query: str) -> List[str]:
        """Free-text search over titles + keywords. An exact RFC-number
        token short-circuits to that single RFC, matching the website."""
        terms = [t for t in clean_string(query).split() if t]
        if not terms:
            return []
        result: Optional[Set[str]] = None
        for term in terms:
            if term.isdigit():
                padded = rfc_num_to_name(term)
                if self.has(padded):
                    return [padded]
            hits = {
                name
                for name in self.all_rfcs
                if self._matches_term(self.rfcs[name], term)
            }
            result = hits if result is None else (result & hits)
        return sorted(result or set())

    def inbound_refs(
        self, name: str, include_obsoleted: bool = False
    ) -> List[Tuple[bool, str]]:
        refs = list(self.in_refs.get(name, []))
        if include_obsoleted:
            for old in self.rfcs.get(name, {}).get("obsoletes", []):
                refs += self.inbound_refs(old, True)
        return refs

    def outbound_refs(self, name: str) -> Dict[str, List[str]]:
        raw = self.refs.get(rfc_name_to_num(name), {})
        return {
            "normative": [rfc_num_to_name(r) for r in raw.get("normative", [])],
            "informative": [rfc_num_to_name(r) for r in raw.get("informative", [])],
        }


# --- Memoised load (mtime-keyed so a re-gather is picked up live) ---------

_CACHE: Optional[Tuple[float, RfcData]] = None


def _load() -> Optional[RfcData]:
    """Return the parsed index, or None if it hasn't been gathered.

    Memoised on `rfcs.json`'s mtime so a long-running MCP server picks up
    a re-gather without a restart, while warm calls skip the parse.
    """
    global _CACHE  # pylint: disable=global-statement
    try:
        mtime = os.path.getmtime(_rfc_file("rfcs.json"))
    except OSError:
        _CACHE = None
        return None
    if _CACHE is not None and _CACHE[0] == mtime:
        return _CACHE[1]
    try:
        rfcs = _read_json("rfcs.json")
        refs = _read_json("refs.json")
        tags = _read_json("tags.json")
    except (OSError, ValueError):
        return None
    data = RfcData(rfcs, refs, tags)
    _CACHE = (mtime, data)
    return data


def _read_json(name: str) -> Dict[str, Any]:
    with open(_rfc_file(name), "r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return loaded if isinstance(loaded, dict) else {}


_NOT_GATHERED = (
    "The RFC index has not been gathered yet. It populates automatically "
    "the next time you run `ietf-llm <corpus>` (any corpus); run one and "
    "retry."
)


def _index_age() -> Optional[float]:
    """Seconds since the mirror was last written, or None if it's absent."""
    try:
        return time.time() - os.path.getmtime(_rfc_file("rfcs.json"))
    except OSError:
        return None


def _format_age(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = max(1, int(seconds // 3600))
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _canonical_name(number: str) -> Optional[str]:
    """`RFC<n>` for any accepted spelling of `number` ("9110", "RFC9110",
    zero-padded), or None when it holds no digits. The one place the
    caller-input normalisation lives, so the lookups below cannot drift."""
    match = re.search(r"\d+", str(number))
    return rfc_num_to_name(match.group(0)) if match else None


def is_stale_miss(number: str) -> bool:
    """True when `number` is absent from the mirror *and* the mirror is past
    its TTL — i.e. the only case where the miss might be staleness rather
    than truth, and so the only case worth spending a live revalidation on.

    False for a hit, for a miss inside the TTL (authoritative), and when
    there's no mirror at all (nothing to revalidate — that's a gather).
    """
    data = _load()
    if data is None:
        return False
    name = _canonical_name(number)
    if not name or data.has(name):
        return False
    age = _index_age()
    return age is not None and age >= RFC_TTL_SECONDS


def working_group(number: str) -> Optional[str]:
    """The WG acronym that published `number`, or None.

    None for the ~36% of the series with no `wg` in the mirror — the legacy
    and independent streams, and anything predating group attribution. A
    caller looking for the RFC's body uses this to pick the one corpus worth
    a bounded lookup; it is a hint, not a guarantee, since a WG may publish
    an RFC that no corpus of that name was ever gathered for.
    """
    data = _load()
    name = _canonical_name(number) if data is not None else None
    if data is None or not name:
        return None
    wg = data.rfcs.get(name, {}).get("wg")
    return str(wg) if wg else None


def no_such_rfc(number: str) -> str:
    """Render a miss.

    A miss has two indistinguishable causes: the number really isn't in the
    series, or it was published since we last mirrored the index. Past the
    TTL we can't tell, so say so rather than assert a bare negative the
    caller would read as authoritative — RFCs don't publish in number order,
    so a stale mirror has *scattered* holes, not a missing tail, and "9845
    and 9847 are here" is no evidence that 9846 isn't real.
    """
    message = f"No such RFC: `{number}`"
    age = _index_age()
    if age is None or age < RFC_TTL_SECONDS:
        return message
    return (
        f"{message} — but the local RFC index was last refreshed "
        f"{_format_age(age)} ago, so an RFC published since then would be "
        "missing from it. Re-gather (`ietf-llm <corpus>`, any corpus) to "
        "refresh the index and retry before concluding it doesn't exist."
    )


# --- Filtering + rendering ------------------------------------------------


def _apply_filters(
    data: RfcData,
    names: List[str],
    status: Optional[str],
    stream: Optional[str],
    level: Optional[str],
    wg: Optional[str],
) -> List[str]:
    wanted = {"status": status, "stream": stream, "level": level, "wg": wg}
    active = {k: v for k, v in wanted.items() if v}
    if not active:
        return names
    return [
        name
        for name in names
        if all(data.rfcs[name].get(field) == value for field, value in active.items())
    ]


def _facets(rfc: Dict[str, Any]) -> str:
    bits = [rfc.get("status"), rfc.get("stream"), rfc.get("level")]
    if rfc.get("wg"):
        bits.append(rfc["wg"])
    return " · ".join(b for b in bits if b)


def render_search(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    query: str,
    status: Optional[str] = None,
    stream: Optional[str] = None,
    level: Optional[str] = None,
    group: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Search the RFC series; return a compact markdown list. A bare RFC
    number returns that single RFC."""
    data = _load()
    if data is None:
        return _NOT_GATHERED
    names = _apply_filters(data, data.search(query), status, stream, level, group)
    total = len(names)
    shown = names[: max(0, limit)]
    if not shown:
        return f"No RFCs match `{query}`" + (
            " with those filters." if total == 0 else "."
        )
    header = f"**{total} RFC{'s' if total != 1 else ''}**"
    if len(shown) < total:
        header += f" (showing {len(shown)})"
    lines = [header, ""]
    for name in shown:
        rfc = data.rfcs[name]
        facets = _facets(rfc)
        suffix = f" · {facets}" if facets else ""
        lines.append(f"- **{name}** — {rfc.get('title', '(untitled)')}{suffix}")
    return "\n".join(lines)


def _ref_list(names: List[str], limit: int = 30) -> str:
    if not names:
        return "—"
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", … (+{len(names) - limit} more)"


def render_rfc(number: str) -> str:
    """Full metadata for one RFC as markdown: facets, keywords, the
    obsoletes graph, references in and out, and links to the text."""
    data = _load()
    if data is None:
        return _NOT_GATHERED
    match = re.search(r"\d+", str(number))
    name = rfc_num_to_name(match.group(0)) if match else ""
    if not name or not data.has(name):
        return no_such_rfc(number)
    rfc = data.rfcs[name]
    num = rfc_name_to_num(name)
    inbound = data.inbound_refs(name)
    outbound = data.outbound_refs(name)
    norm_cites = sum(1 for is_norm, _ in inbound if is_norm)
    info_cites = len(inbound) - norm_cites
    lines = [
        f"## {name} — {rfc.get('title', '(untitled)')}",
        "",
        f"- Status: {rfc.get('status', 'unknown')}",
        f"- Stream: {rfc.get('stream', 'unknown')}",
        f"- Level: {rfc.get('level', 'unknown')}",
    ]
    if rfc.get("wg"):
        lines.append(f"- Working group: {rfc['wg']}")
    if rfc.get("area"):
        lines.append(f"- Area: {rfc['area']}")
    if rfc.get("keywords"):
        lines.append(f"- Keywords: {', '.join(rfc['keywords'])}")
    lines += [
        f"- Obsoletes: {_ref_list(rfc.get('obsoletes', []))}",
        f"- Obsoleted by: {_ref_list(data.obsoleted_by.get(name, []))}",
        f"- References (normative): {_ref_list(outbound['normative'])}",
        f"- References (informative): {_ref_list(outbound['informative'])}",
        f"- Cited by: {norm_cites} normative, {info_cites} informative",
        f"- Text: {_TEXT_BASE}/rfc{num}.txt",
        f"- Info: {_INFO_BASE}/rfc{num}",
    ]
    return "\n".join(lines)
