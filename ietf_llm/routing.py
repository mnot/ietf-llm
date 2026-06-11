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
from typing import Any, Dict, List, Optional, cast

import numpy as np

from .embeddings.storage import decode_centroid
from .kv_store import ABSENT, KvStore
from .utils import Verbosity

#: Cosine floor (on **mean-centered** scores) for a confident match: below it,
#: routing abstains rather than name a least-bad corpus. Provisional — sentence
#: embedding models (bge-small, the default) are anisotropic, so raw cosines
#: between unrelated texts cluster near ~0.95 and carry no usable gradient;
#: `_score_group` removes that common-mode by mean-centering, which restores a
#: spread, but the exact floor wants calibration against live queries per model.
#: Override with IETF_LLM_ROUTING_MIN_SCORE. See tests/test_routing.py.
_BUILTIN_MIN_SCORE = 0.5


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
#: clobber), mirroring the shared identity maps in `gather/cache_sync.py`.
FLEET_ROUTING_KEY = "fleet/routing/centroids.json"

#: Bounded retries for the fleet-key compare-and-swap (mirrors `kv_control`).
_CAS_RETRIES = 8


@dataclass
class RoutingEntry:
    """One corpus's routing centroids, decoded for scoring."""

    model_id: str
    dim: int
    centroids: "np.ndarray[Any, np.dtype[np.float32]]"  # (k, dim), L2-normalised


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
    from .corpus_store import (  # pylint: disable=import-outside-toplevel
        get_corpus_store,
    )

    store = get_corpus_store()
    # The cloud backend hands back its one fleet key; the local backend returns
    # None, so we scan each corpus's topics.json ourselves (the embeddings read
    # lives here, not in corpus_store, to keep that module off the embeddings
    # import graph).
    known_list = store.list_corpora()
    raw_table = store.routing_fleet_table()
    if raw_table is None:
        raw_table = _scan_local(known_list)
    known = set(known_list)
    table: Dict[str, RoutingEntry] = {}
    for corpus, raw in raw_table.items():
        entry = _entry_from_raw(raw) if isinstance(raw, dict) else None
        if entry is not None:
            table[corpus] = entry
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
    mean = np.vstack([table[c].centroids for c in usable]).mean(axis=0)
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


def _scan_local(corpora: List[str]) -> Dict[str, Any]:
    """Assemble the routing table from local `topics.json` sidecars — the local
    backend's path (the cloud backend supplies a fleet key instead). Reads
    `embeddings` here so `corpus_store` stays off that import graph."""
    from .embeddings.storage import (
        read_topics,
    )  # pylint: disable=import-outside-toplevel
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
