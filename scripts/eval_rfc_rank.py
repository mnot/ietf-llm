#!/usr/bin/env python
"""Re-measure the document-ranking coefficients in `singletons/rfc_rank.py`
against the LIVE embedding model and rfc.fyi's published index (issue #230).

The coefficients there are swept, not guessed, and a swept number that can't
be re-swept decays into a magic constant. This is how you check one.

The serve venv is torch-free, so run it with a Python that HAS the embedding
model installed, pointed at a populated `~/.cache/ietf-llm` (it needs the
`_rfc/` mirror for titles and the citation graph):

    .venv/bin/python scripts/eval_rfc_rank.py --index path/to/index
    .venv/bin/python scripts/eval_rfc_rank.py --download   # fetch the latest

It writes nothing. `--queries` wants rfc.fyi's `eval/queries.json`; the labels
are theirs and the set is not vendored here.

Retrieval is held fixed across arms -- a brute-force scan with the bge query
prefix, which is what our search does -- so the difference between arms is
the aggregation and nothing else. Note this makes the numbers *not* directly
comparable to rfc.fyi's, which are measured through the browser's IVF at
nprobe=20 and so start from a lower retrieval ceiling.

    --sweep re-runs the title and citation weights over a grid, which is how
    the 0.10 / 0.02 defaults were chosen. Watch for the cliffs: upstream
    found title 0.20 scoring *worse* than no title signal, and citation 0.05
    losing recall by promoting well-cited RFCs on citation count alone.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ietf_llm.embeddings.models import (
    DEFAULT_EMBED_MODEL,
    _get_embed_model,
    query_prefix,
)
from ietf_llm.log import Verbosity
from ietf_llm.rfcindex import (
    download_index,
    iter_clusters,
    latest_release,
    read_manifest,
)
from ietf_llm.singletons import rfcs as rfcdata
from ietf_llm.singletons.rfc_rank import (
    DEFAULT_CITATION_WEIGHT,
    DEFAULT_TITLE_WEIGHT,
    rank_documents,
)

TOP_CHUNKS = 200
TOP_DOCS = 10
_BLOCK = 50_000


class Hit:
    __slots__ = ("rfc", "score")

    def __init__(self, rfc: str, score: float) -> None:
        self.rfc = rfc
        self.score = score


def load_index(index_dir: str) -> Any:
    manifest = read_manifest(index_dir)
    blocks, rfcs = [], []
    for cluster in iter_clusters(index_dir, manifest):
        if not cluster.chunks:
            continue
        blocks.append(cluster.vectors)
        rfcs.extend(c.rfc for c in cluster.chunks)
    return manifest, np.vstack(blocks), np.array(rfcs, dtype=object)


def score_all(mat: Any, scale: float, vec: Any) -> Any:
    sims = np.empty(mat.shape[0], dtype=np.float32)
    for lo in range(0, mat.shape[0], _BLOCK):
        blk = mat[lo : lo + _BLOCK].astype(np.float32) * np.float32(scale)
        sims[lo : lo + _BLOCK] = blk @ vec
    return sims


def evaluate(
    orders: Sequence[List[str]], queries: Sequence[Dict[str, Any]]
) -> Dict[str, float]:
    at_k, rr, first = [], [], []
    for order, query in zip(orders, queries):
        want = {str(n) for n in query["relevant_rfcs"]}
        at_k.append(1.0 if want & set(order[:TOP_DOCS]) else 0.0)
        pos = next((i for i, d in enumerate(order, 1) if d in want), None)
        rr.append(1.0 / pos if pos else 0.0)
        if pos:
            first.append(pos)
    return {
        "recall@10": float(np.mean(at_k)),
        "mrr": float(np.mean(rr)),
        "median_rank": float(statistics.median(first)) if first else float("nan"),
    }


def main() -> None:  # pylint: disable=too-many-locals
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", help="an unpacked published index")
    ap.add_argument("--download", action="store_true", help="fetch the latest release")
    ap.add_argument("--queries", default="../rfc.fyi/eval/queries.json")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    index_dir: Optional[str] = args.index
    tmp: Optional[tempfile.TemporaryDirectory] = None  # type: ignore[type-arg]
    if args.download:
        release = latest_release()
        if release is None:
            raise SystemExit("no published index release")
        tmp = tempfile.TemporaryDirectory()
        print(f"fetching {release.tag}", file=sys.stderr)
        index_dir = download_index(release, tmp.name)
    if not index_dir or not os.path.isdir(index_dir):
        raise SystemExit("need --index <dir> or --download")

    manifest, mat, rfc_of = load_index(index_dir)
    print(f"{mat.shape[0]:,} chunks from {manifest.build}", file=sys.stderr)

    data = rfcdata._load()  # pylint: disable=protected-access
    if data is None:
        raise SystemExit("no _rfc mirror in the cache; run a gather first")
    cites: Dict[str, int] = {}

    def title_of(num: str) -> str:
        return str((data.rfcs.get(f"RFC{num}") or {}).get("title", ""))

    def citations_of(num: str) -> int:
        name = f"RFC{num}"
        if name not in cites:
            cites[name] = len(data.inbound_refs(name, True))
        return cites[name]

    with open(args.queries, encoding="utf-8") as fh:
        queries = [q for q in json.load(fh)["queries"] if q.get("relevant_rfcs")]

    model = _get_embed_model(DEFAULT_EMBED_MODEL, Verbosity.QUIET)
    prefix = query_prefix(DEFAULT_EMBED_MODEL)
    per_query: List[List[Hit]] = []
    for query in queries:
        vec = np.asarray(list(model.embed(prefix + query["query"])), dtype=np.float32)
        vec /= np.linalg.norm(vec)
        sims = score_all(mat, manifest.scale, vec)
        idx = np.argpartition(-sims, TOP_CHUNKS)[:TOP_CHUNKS]
        per_query.append([Hit(str(rfc_of[i]), float(sims[i])) for i in idx])

    def ranked_orders(title_w: float, cite_w: float) -> List[List[str]]:
        out = []
        for query, hits in zip(queries, per_query):
            ranked = rank_documents(
                query["query"],
                hits,
                doc_of=lambda h: h.rfc,
                score_of=lambda h: h.score,
                title_of=title_of,
                citations_of=citations_of,
                title_weight=title_w,
                citation_weight=cite_w,
            )
            out.append([r.doc for r in ranked])
        return out

    best_passage = []
    for hits in per_query:
        best: Dict[str, float] = {}
        for hit in hits:
            best[hit.rfc] = max(best.get(hit.rfc, -9.0), hit.score)
        best_passage.append([d for d, _ in sorted(best.items(), key=lambda kv: -kv[1])])

    rows = [("best-passage", evaluate(best_passage, queries))]
    rows.append(
        (
            f"reranked ({DEFAULT_TITLE_WEIGHT}/{DEFAULT_CITATION_WEIGHT})",
            evaluate(
                ranked_orders(DEFAULT_TITLE_WEIGHT, DEFAULT_CITATION_WEIGHT), queries
            ),
        )
    )
    if args.sweep:
        for title_w in (0.0, 0.05, 0.10, 0.20):
            for cite_w in (0.0, 0.02, 0.05):
                rows.append(
                    (
                        f"  title={title_w} cite={cite_w}",
                        evaluate(ranked_orders(title_w, cite_w), queries),
                    )
                )

    print(f"{'arm':<28}{'recall@10':>12}{'MRR':>10}{'median rank':>14}")
    for label, res in rows:
        print(
            f"{label:<28}{res['recall@10']:>12.3f}{res['mrr']:>10.3f}"
            f"{res['median_rank']:>14.0f}"
        )
    print(f"\n{len(queries)} labelled queries, top {TOP_CHUNKS} chunks aggregated")
    if tmp is not None:
        tmp.cleanup()


if __name__ == "__main__":
    main()
