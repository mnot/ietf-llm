"""Per-corpus centroid routing (issue #116, item 2).

"Which gathered corpus is this question about?" — the signal `find_efforts`
(keyword over the catalog) and `search_corpus` (needs a named corpus) can't
give. Embed the question once, score every gathered corpus by the **max**
cosine over its topic-map centroids (a single corpus mean washes out a broad
WG; max keeps each theme its own attractor), rank, and **abstain** when nothing
is close.

The centroids are the topic map's (`embeddings/topics.py`), reused with no
extra gather work. The cross-corpus centroid set is assembled through the
`CorpusStore.routing_fleet_table` seam — None on the local backend, so this
module scans each corpus's `topics.json`; the one `fleet/routing` key on the
cloud backend (this module owns that key's read/write). Scores are comparable
only within one embedding-model id (cosine isn't portable across backends — the
same gate `search_corpora` enforces via `index_model`), so routing scores the
largest single-model group and reports the rest as skipped.

Reader-side and offline apart from embedding the query (the same dependency
`search` already has); the centroid set is produced write-side at gather time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, cast

import numpy as np

from .embeddings.storage import decode_centroid
from .store.kv import ABSENT, KvStore
from .utils import Verbosity

#: Cosine floor (on **mean-centered** scores) for a confident match: below it,
#: routing abstains rather than name a least-bad corpus. Calibrated for the
#: default bge-small model with `scripts/calibrate_routing.py` over the gathered
#: corpora: off-topic queries top out around ~0.24 and on-topic queries sit at a
#: median ~0.48, so 0.30 clears the off-topic ceiling with margin while keeping
#: the clearly on-topic hits. (Sentence embedders are anisotropic — raw cosines
#: between unrelated texts cluster near ~0.95 with no usable gradient;
#: `_score_group` mean-centers to remove that common-mode, which is what makes
#: any absolute floor meaningful.) Swap the embedder → recalibrate; override
#: per deployment with IETF_LLM_ROUTING_MIN_SCORE.
_BUILTIN_MIN_SCORE = 0.30


def _env_min_score() -> float:
    raw = os.environ.get("IETF_LLM_ROUTING_MIN_SCORE", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _BUILTIN_MIN_SCORE


#: Resolved once at import for the tool's display; `route` reads the same value.
DEFAULT_MIN_SCORE = _env_min_score()

#: One fleet-wide key holds every corpus's routing entry on the cloud backend,
#: so a reader routes the whole fleet with a single GET instead of materialising
#: each corpus's version to read its sidecar. Each gather merges *its own* entry
#: under compare-and-swap (concurrent publishers of different corpora must not
#: clobber), mirroring the shared identity maps in `gather/sources/cache_sync.py`.
FLEET_ROUTING_KEY = "fleet/routing/centroids.json"

#: Bounded retries for the fleet-key compare-and-swap (mirrors `store.control`).
_CAS_RETRIES = 8

#: Cross-corpus generic-theme suppression (issue #116 follow-on). A theme that
#: recurs across a large fraction of the fleet is process boilerplate (meeting
#: logistics, ballots, document preamble), not distinctive discussion — `overview`
#: demotes it. The numbers are calibrated on bge-small over the gathered corpora
#: (centroid-to-centroid, no live model needed): two themes count as "the same"
#: above `_GENERIC_TAU` mean-centered cosine, and a theme is generic when ≥
#: `_GENERIC_UBIQUITY` of the *other* same-model corpora carry a match. At that
#: τ, meeting/procedural themes sit at ubiquity 0.6–0.8 while a cross-cutting
#: *substantive* topic shared by only a corpus or two (e.g. preferences) stays
#: ~0.2 — so "shared" doesn't get mistaken for "generic". Recalibrate on a model
#: swap. Below `_GENERIC_MIN_CORPORA` same-model corpora the fraction is too
#: coarse to trust, so suppression stays off (a small / single-corpus deployment
#: is unaffected; the gather-time bot-filter remains the floor).
_GENERIC_TAU = 0.5
_GENERIC_UBIQUITY = 0.6
_GENERIC_MIN_CORPORA = 5


@dataclass
class RoutingEntry:
    """One corpus's routing centroids, decoded for scoring."""

    model_id: str
    dim: int
    # (k, dim) cluster centroids. Means of unit vectors, so not themselves
    # unit-norm — `_score_group` re-normalises (after mean-centering) before the
    # dot product, so the stored magnitude doesn't matter.
    centroids: "np.ndarray[Any, np.dtype[np.float32]]"


@dataclass
class CorpusMatch:
    corpus: str
    score: float  # max cosine of the query against this corpus's centroids


@dataclass
class RouteResult:
    """The outcome of a routing query. `confident` is whether the top match
    cleared the abstention floor; a caller should fall back to `find_efforts`
    when it is False even if `matches` is non-empty."""

    matches: List[CorpusMatch]  # ranked desc, within the scored model group
    confident: bool
    model_id: str  # the embedding model the query was scored against ("" if none)
    skipped_other_model: List[str] = field(default_factory=list)
    no_centroids: List[str] = field(default_factory=list)
    error: Optional[str] = None  # set when the query could not be embedded


def _entry_from_raw(raw: Dict[str, Any]) -> Optional[RoutingEntry]:
    """Decode one stored routing entry ({model_id, dim, centroids:[b64,...]}) to
    a `RoutingEntry`, or None when it carries no usable centroids / model id."""
    model = raw.get("model_id")
    blobs = raw.get("centroids") or []
    if not model or not blobs:
        return None
    try:
        mat = np.vstack([decode_centroid(b) for b in blobs]).astype(np.float32)
    except (ValueError, TypeError):
        return None
    return RoutingEntry(str(model), int(raw.get("dim") or mat.shape[1]), mat)


def _load_table() -> "Tuple[Dict[str, RoutingEntry], set[str]]":
    """Load the decoded routing table and the set of currently-cached corpora.

    The cloud backend hands back its one fleet key; the local backend returns
    None, so we scan each corpus's `topics.json` (the embeddings read lives
    here, not in `store.corpus`, to keep that module off the embeddings import
    graph). The table is intersected with the cached set: the cloud key is
    additive per publish, so a removed corpus can linger there and must not be
    scored (the local scan is already cache-bounded)."""
    from .store.corpus import (  # pylint: disable=import-outside-toplevel
        get_corpus_store,
    )

    store = get_corpus_store()
    known_list = store.list_corpora()
    raw_table = store.routing_fleet_table()
    if raw_table is None:
        raw_table = _scan_local(known_list)
    known = set(known_list)
    table: Dict[str, RoutingEntry] = {}
    for corpus, raw in raw_table.items():
        if corpus not in known:
            continue
        entry = _entry_from_raw(raw) if isinstance(raw, dict) else None
        if entry is not None:
            table[corpus] = entry
    return table, known


def route(
    query: str,
    *,
    limit: int = 8,
    min_score: float = DEFAULT_MIN_SCORE,
    verbose: Verbosity = Verbosity.QUIET,
) -> RouteResult:
    """Rank gathered corpora by topic-centroid similarity to `query`.

    Embeds `query` once with the embedding model the largest group of corpora
    share, scores those corpora (max cosine over their centroids), and ranks
    them. Corpora on a different model id are reported as skipped, not mixed in.
    """
    table, known = _load_table()
    no_centroids = sorted(known - set(table))
    if not table:
        return RouteResult([], False, "", [], no_centroids)

    # Group by embedding-model id; score the largest group (ties → model id, so
    # the choice is deterministic). Cosine scores only compare within a group.
    groups: Dict[str, List[str]] = {}
    for corpus, entry in table.items():
        groups.setdefault(entry.model_id, []).append(corpus)
    chosen = max(groups, key=lambda m: (len(groups[m]), m))
    skipped = sorted(c for m, members in groups.items() if m != chosen for c in members)

    q_vec = _embed_query(chosen, query, verbose)
    if q_vec is None:
        return RouteResult([], False, chosen, skipped, no_centroids, "embed-failed")

    matches = _score_group(query=q_vec, corpora=groups[chosen], table=table)
    matches.sort(key=lambda m: (-m.score, m.corpus))
    matches = matches[:limit]
    confident = bool(matches and matches[0].score >= min_score)
    return RouteResult(matches, confident, chosen, skipped, no_centroids)


def _score_group(
    *,
    query: "np.ndarray[Any, np.dtype[np.float32]]",
    corpora: List[str],
    table: Dict[str, RoutingEntry],
) -> List[CorpusMatch]:
    """Score each corpus as the max cosine of `query` against its centroids, on
    **mean-centered** vectors.

    Sentence embedding models are anisotropic: every vector carries a large
    shared common-mode component, so raw cosines between unrelated texts sit
    near ~0.95 and barely move with topic. Subtracting the mean of the group's
    centroids (the common-mode direction) and renormalising restores a usable
    gradient, so the abstention floor means something. The mean is over the
    same-model group only — the one set whose geometry is comparable.
    """
    usable = [c for c in corpora if table[c].centroids.shape[1] == query.shape[0]]
    if not usable:
        return []
    mean = (
        np.vstack([table[c].centroids for c in usable]).mean(axis=0).astype(np.float32)
    )
    q_centered = _recenter(query[None, :], mean)[0]
    matches: List[CorpusMatch] = []
    for corpus in usable:
        centered = _recenter(table[corpus].centroids, mean)
        matches.append(CorpusMatch(corpus, float((centered @ q_centered).max())))
    return matches


def _recenter(
    mat: "np.ndarray[Any, np.dtype[np.float32]]",
    mean: "np.ndarray[Any, np.dtype[np.float32]]",
) -> "np.ndarray[Any, np.dtype[np.float32]]":
    """Subtract `mean` from each row and L2-renormalise (zero rows left as-is),
    so a dot product against the result is a cosine in the centered space."""
    shifted = mat - mean
    norms = np.linalg.norm(shifted, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return cast(
        "np.ndarray[Any, np.dtype[np.float32]]", (shifted / norms).astype(np.float32)
    )


def generic_theme_flags(corpus: str) -> Optional[List[bool]]:
    """Per-theme "is this generic across the fleet?" flags for `corpus`, aligned
    to its `topics.json` cluster order, or None when suppression does not apply.

    A theme is generic when a near-match for it appears in at least
    `_GENERIC_UBIQUITY` of the *other* same-model corpora — process boilerplate
    (meeting logistics, ballots) recurs everywhere; a distinctive theme does
    not. `overview` uses this to demote (not drop) generic themes. Returns None
    — leaving the topic map untouched — when the corpus has no centroids, or
    there are fewer than `_GENERIC_MIN_CORPORA` same-model corpora to compare
    against (the cross-corpus signal needs breadth; a small deployment relies on
    the gather-time bot-filter instead). Reader-side, recomputed per call so it
    tracks the current fleet without a re-gather.
    """
    table, _known = _load_table()
    me = table.get(corpus)
    if me is None:
        return None
    group = {
        name: entry
        for name, entry in table.items()
        if entry.model_id == me.model_id
        and entry.centroids.shape[1] == me.centroids.shape[1]
    }
    if len(group) < _GENERIC_MIN_CORPORA:
        return None
    mean = (
        np.vstack([entry.centroids for entry in group.values()])
        .mean(axis=0)
        .astype(np.float32)
    )
    mine = _recenter(me.centroids, mean)
    others = [
        _recenter(entry.centroids, mean)
        for name, entry in group.items()
        if name != corpus
    ]
    if not others:
        return None
    flags: List[bool] = []
    for theme in mine:
        shared = sum(1 for oc in others if float((oc @ theme).max()) >= _GENERIC_TAU)
        flags.append(shared / len(others) >= _GENERIC_UBIQUITY)
    return flags


def _scan_local(corpora: List[str]) -> Dict[str, Any]:
    """Assemble the routing table from local `topics.json` sidecars — the local
    backend's path (the cloud backend supplies a fleet key instead). Reads
    `embeddings` here so `store.corpus` stays off that import graph."""
    from .embeddings.storage import (  # pylint: disable=import-outside-toplevel
        read_topics,
    )
    from .embeddings.topics import (  # pylint: disable=import-outside-toplevel
        routing_projection,
    )

    out: Dict[str, Any] = {}
    for corpus in corpora:
        topics = read_topics(corpus)
        if not topics:
            continue
        entry = routing_projection(topics)
        if entry is not None:
            out[corpus] = entry
    return out


def _embed_query(
    model_id: str, query: str, verbose: Verbosity
) -> Optional["np.ndarray[Any, np.dtype[np.float32]]"]:
    """Embed and L2-normalise `query` with `model_id`, or None if the model
    can't load or the embed fails (same provider-variability story as
    `search`)."""
    from .embeddings.models import (  # pylint: disable=import-outside-toplevel
        _get_embed_model,
    )

    model = _get_embed_model(model_id, verbose)
    if model is None:
        return None
    try:
        vec = np.asarray(list(model.embed(query)), dtype=np.float32)
    except Exception:  # pylint: disable=broad-except
        return None
    norm = float(np.linalg.norm(vec))
    if norm:
        vec = vec / norm
    return vec


# --- cloud fleet key (read at routing time, merged at publish time) --------


def read_fleet_table(kv: KvStore) -> Dict[str, Any]:
    """The whole fleet routing table from the cloud KvStore — `{corpus: raw
    entry}` — or `{}` on a miss / unreadable value. One GET routes the fleet."""
    record = kv.get(FLEET_ROUTING_KEY)
    if record is None:
        return {}
    try:
        parsed = json.loads(record[0])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def persist_corpus_entry(kv: KvStore, corpus: str, entry: Dict[str, Any]) -> None:
    """Merge one corpus's routing `entry` into the fleet key under compare-and-
    swap. Lossless under concurrent publishers of *different* corpora: a writer
    that loses the race re-reads and re-merges. Best-effort — never raises."""
    try:
        for _ in range(_CAS_RETRIES):
            record = kv.get(FLEET_ROUTING_KEY)
            if record is None:
                remote: Dict[str, Any] = {}
                expect: object = ABSENT
            else:
                parsed = json.loads(record[0])
                remote = parsed if isinstance(parsed, dict) else {}
                expect = record[1]
            remote[corpus] = entry
            payload = json.dumps(remote, sort_keys=True).encode("utf-8")
            if kv.put(FLEET_ROUTING_KEY, payload, expect=expect) is not None:
                return
    except Exception:  # pylint: disable=broad-except
        pass
