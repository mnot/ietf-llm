"""Topic map: cluster a corpus's documents into themes and label them.

Runs at gather time (after `build_index`) and writes a `topics.json` sidecar
beside the index; `overview` renders it, and centroid routing (issue #116,
item 2) later reuses the centroids. Clustering is over per-file mean-pooled
vectors (`storage.load_documents`), not raw chunks, so a busy thread doesn't
splinter into many near-identical themes.

Each cluster is labelled two ways, both cheap and offline: a tf-idf keyword
fingerprint (the terms that distinguish this cluster from the rest of the
corpus) and the titles of the documents nearest its centroid (concrete
anchors). Recency is carried per cluster as `last_active` (the newest member
date) — annotated, not used to restrict the clustering, so the full archive
stays clustered and routing keeps complete coverage.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Any, Dict, List, Optional

import numpy as np

from ..log import LogLevel, Verbosity, log
from .clustering import choose_k, mini_batch_kmeans
from .search import index_model
from .storage import (
    Document,
    _topics_path,
    encode_centroid,
    load_documents,
    write_topics,
)


def has_topics(wg: str) -> bool:
    """True if `wg`'s topic-map sidecar already exists (write side). The gather
    skips regenerating the topic map when the index did not change, but only when
    a sidecar is already present, so a first build (or one whose earlier topic
    step failed) still gets one (issue #190)."""
    return os.path.isfile(_topics_path(wg, write=True))


#: Below this many documents a corpus is too small to theme usefully — skip
#: the topic map rather than emit one-document "themes".
_MIN_DOCS = 8
#: Top tf-idf terms kept per cluster.
_MAX_TERMS = 6
#: Document titles surfaced per cluster as concrete anchors.
_MAX_EXEMPLARS = 3
#: Terms shorter than this are dropped (after stripping) as noise.
_MIN_TERM_LEN = 3

#: Schema version for `topics.json`, so a future reader can tell an old
#: sidecar from a new one without re-gathering everything blindly.
TOPICS_VERSION = 1

_WORD_RE = re.compile(r"[a-z][a-z0-9][a-z0-9-]+")

#: Subjects of machine-generated mailing-list notices (the Datatracker /
#: IESG / Secretariat bots). They aren't discussion — left in, they form
#: large look-alike clusters that crowd out the real themes (a single WG's
#: "I-D Action:" announcements can be 10%+ of its documents) — so the topic
#: map drops them before clustering. Anchored at the subject start; matched
#: case-insensitively. The underlying messages stay in the index and are
#: still searchable; only the topic map ignores them.
_AUTOMATED_SUBJECT_RE = re.compile(
    r"^\s*(i-d action|protocol action|document action|wg action"
    r"|new version notification|publication has been requested"
    r"|the ietf datatracker)\b",
    re.IGNORECASE,
)


def _is_automated(doc: Document) -> bool:
    """True if `doc` is an automated notice (see `_AUTOMATED_SUBJECT_RE`)."""
    return bool(_AUTOMATED_SUBJECT_RE.match(doc.title))


#: Generic English + mailing-list/IETF boilerplate that carries no topical
#: signal. Kept small and hand-picked — tf-idf already demotes corpus-wide
#: terms, this just removes the words that survive it by sheer frequency.
_STOPWORDS = frozenset("""
    the and for that this with you your are not but they have has had was were
    will would should could can may might must from out who whom which what when
    where why how all any some such only also then than them their there these
    those been being does did done into onto over under more most much many one
    two get got see use used using like just well good make made does don doesn
    isn aren wasn weren about above below after before again here once each other
    draft drafts ietf wg working group mailing list email mail message thread
    issue issues github comment comments subject sent wrote writes regards thanks
    hello hi re fwd cheers best please think i'd i'm it's that's
    """.split())


def _tokenize(text: str) -> List[str]:
    return [
        w
        for w in _WORD_RE.findall(text.lower())
        if len(w) >= _MIN_TERM_LEN and w not in _STOPWORDS
    ]


def _label_clusters(
    docs: List[Document], assignments: "np.ndarray[Any, np.dtype[np.intp]]", k: int
) -> List[List[str]]:
    """Top tf-idf terms per cluster.

    Term frequency is summed over a cluster's member documents; document
    frequency is counted across all documents (one count per doc, so a term
    repeated within a doc doesn't inflate its idf). A term's cluster score is
    `tf · log(n_docs / df)`, so a word common to the whole corpus scores near
    zero and a word concentrated in one cluster rises to the top.
    """
    n_docs = len(docs)
    doc_tokens = [_tokenize(d.text + " " + d.title) for d in docs]
    doc_freq: Counter[str] = Counter()
    for tokens in doc_tokens:
        doc_freq.update(set(tokens))
    cluster_tf: List[Counter[str]] = [Counter() for _ in range(k)]
    for i, tokens in enumerate(doc_tokens):
        cluster_tf[int(assignments[i])].update(tokens)
    labels: List[List[str]] = []
    for tf in cluster_tf:
        scored = [
            (count * math.log(n_docs / doc_freq[term]), term)
            for term, count in tf.items()
            # df == n_docs ⇒ idf 0 (term in every doc); skip it outright.
            if doc_freq[term] < n_docs
        ]
        scored.sort(key=lambda st: (-st[0], st[1]))
        labels.append([term for _, term in scored[:_MAX_TERMS]])
    return labels


def _exemplars(
    docs: List[Document],
    assignments: "np.ndarray[Any, np.dtype[np.intp]]",
    centroids: "np.ndarray[Any, np.dtype[np.float32]]",
    cluster: int,
) -> List[str]:
    """Titles of the cluster's documents nearest its centroid (de-duplicated,
    most-central first)."""
    members = [i for i in range(len(docs)) if int(assignments[i]) == cluster]
    members.sort(key=lambda i: -float(docs[i].vector @ centroids[cluster]))
    seen: set[str] = set()
    out: List[str] = []
    for i in members:
        title = docs[i].title.strip()
        if title and title not in seen:
            seen.add(title)
            out.append(title)
        if len(out) >= _MAX_EXEMPLARS:
            break
    return out


def build_topics(wg: str) -> Optional[Dict[str, Any]]:
    """Compute the topic-map payload for `wg`, or None when there is no index,
    too few documents, or no model recorded. Pure read of the index — the
    caller persists the result with `write_topics`."""
    model = index_model(wg)
    if model is None:
        return None
    # Cluster real discussion only — drop machine-generated notices, which
    # otherwise dominate with look-alike clusters (see _AUTOMATED_SUBJECT_RE).
    docs = [d for d in load_documents(wg) if not _is_automated(d)]
    if len(docs) < _MIN_DOCS:
        return None
    matrix = np.vstack([d.vector for d in docs]).astype(np.float32)
    k = choose_k(len(docs))
    centroids, assignments = mini_batch_kmeans(matrix, k)
    k = centroids.shape[0]  # may be capped to n_docs
    term_labels = _label_clusters(docs, assignments, k)

    clusters: List[Dict[str, Any]] = []
    for ci in range(k):
        members = [i for i in range(len(docs)) if int(assignments[i]) == ci]
        if not members:
            continue
        dates = [la for i in members if (la := docs[i].last_active)]
        terms = term_labels[ci]
        clusters.append(
            {
                "centroid": encode_centroid(centroids[ci]),
                "size": len(members),
                "label": ", ".join(terms[:3]),
                "terms": terms,
                "exemplars": _exemplars(docs, assignments, centroids, ci),
                "last_active": max(dates) if dates else None,
            }
        )
    # Largest themes first — that's the reading order `overview` wants.
    clusters.sort(key=lambda cl: -cl["size"])
    return {
        "version": TOPICS_VERSION,
        "model_id": model,
        "dim": int(matrix.shape[1]),
        "doc_unit": "file",
        "n_docs": len(docs),
        "clusters": clusters,
    }


def routing_projection(topics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Project a parsed `topics.json` to the slim entry centroid routing needs
    (issue #116, item 2): just the provenance and the cluster centroids, no
    labels/exemplars. None when the sidecar carries no usable centroids or no
    model id (an entry with either is useless for routing). The centroids stay
    base64 packed float32 — the same form `topics.json` and the cloud fleet key
    store, decoded only at scoring time."""
    model = topics.get("model_id")
    clusters = topics.get("clusters") or []
    centroids = [c["centroid"] for c in clusters if c.get("centroid")]
    if not model or not centroids:
        return None
    return {"model_id": model, "dim": topics.get("dim"), "centroids": centroids}


def generate_topics(wg: str, verbose: Verbosity = Verbosity.STATUS) -> bool:
    """Build and persist `wg`'s topic map. Returns True if a sidecar was
    written. Best-effort: a corpus too small to theme (or with no index) is a
    quiet no-op, not an error."""
    payload = build_topics(wg)
    if payload is None:
        return False
    write_topics(wg, payload)
    log(
        f"Topic map: {len(payload['clusters'])} themes over "
        f"{payload['n_docs']} documents.",
        verbose,
        level=LogLevel.STATUS,
    )
    return True
