#!/usr/bin/env python
"""Calibrate the centroid-routing abstention floor (issue #116, item 2)
against the LIVE embedding model and your real gathered corpora.

The serve venv is torch-free, so `route` cannot be exercised end to end there.
Run this with a Python that HAS the embedding model installed (your normal
ietf-llm environment, or `pip install sentence-transformers torch` into a venv)
and pointed at a populated `~/.cache/ietf-llm`:

    .venv/bin/python scripts/calibrate_routing.py

It needs no re-gather and writes nothing: it builds each corpus's routing
centroids in memory from its existing `embeddings.db` (the same
`build_topics` the gather runs), then embeds a set of labelled probe queries
with the real model and runs the exact production scoring path
(`routing._embed_query` + `routing._score_group`, mean-centered). It reports
how well on-topic probes route and how strongly off-topic probes are rejected,
then sweeps candidate floors and recommends one.

EDIT the two probe sets below for your gathered corpora — that is the input
that matters. Positives are hand-written (not drawn from the index) on purpose,
so a probe does not score against centroids it helped build.

The recommended value goes in IETF_LLM_ROUTING_MIN_SCORE (and/or
routing._BUILTIN_MIN_SCORE).
"""

from __future__ import annotations

import statistics as st
from typing import Dict, List

import numpy as np

from ietf_llm.corpus import routing
from ietf_llm.embeddings.storage import decode_centroid
from ietf_llm.embeddings.topics import build_topics
from ietf_llm.log import Verbosity

# --- EDIT ME -------------------------------------------------------------- #

# On-topic probes: a query and the corpus it should route to. Hand-written so
# there is no leakage from the index into the score.
POSITIVES: Dict[str, str] = {
    "structured field values for HTTP headers": "httpbis",
    "HTTP caching freshness and revalidation": "httpbis",
    "CONNECT method for tunnelling TCP over HTTP": "httpbis",
    "post-quantum hybrid key exchange in TLS": "tls",
    "deprecating obsolete TLS cipher suites": "tls",
    "Encrypted Client Hello": "tls",
    "expressing AI usage preferences for web crawlers": "aipref",
    "vocabulary for robots.txt content-usage preferences": "aipref",
    "human rights considerations in protocol design": "hrpc",
    "measuring internet path latency and loss": "maprg",
}

# Off-topic probes: routing should ABSTAIN (top score below the floor). Pure
# non-IETF text plus IETF-adjacent topics for groups you have NOT gathered.
NEGATIVES: List[str] = [
    "best pizza restaurants in Rome",
    "how to train a large language model",
    "kubernetes horizontal pod autoscaling",
    "2026 personal income tax filing deadline",
    "recipe for sourdough bread",
]

# Corpora to build the routing table from. Leave empty to use every locally
# cached corpus that has an embedding index.
CORPORA: List[str] = []

# Floor values to sweep (on mean-centered scores).
FLOORS = [round(0.30 + 0.05 * i, 2) for i in range(13)]  # 0.30 … 0.90

# -------------------------------------------------------------------------- #


def _build_table() -> Dict[str, routing.RoutingEntry]:
    from ietf_llm.paths import cached_wg_names  # pylint: disable=import-outside-toplevel

    names = CORPORA or cached_wg_names()
    table: Dict[str, routing.RoutingEntry] = {}
    for corpus in names:
        payload = build_topics(corpus)
        if not payload:
            continue
        mat = np.vstack(
            [decode_centroid(c["centroid"]) for c in payload["clusters"]]
        ).astype(np.float32)
        table[corpus] = routing.RoutingEntry(payload["model_id"], payload["dim"], mat)
    return table


def _ranked(table: Dict[str, routing.RoutingEntry], model_id: str, query: str):
    q = routing._embed_query(model_id, query, Verbosity.QUIET)  # pylint: disable=protected-access
    if q is None:
        raise SystemExit(
            "Could not embed a probe — is the embedding model installed in THIS "
            "Python? (the serve venv is torch-free; use your ietf-llm env)."
        )
    matches = routing._score_group(  # pylint: disable=protected-access
        query=q, corpora=list(table), table=table
    )
    matches.sort(key=lambda m: -m.score)
    return matches


def main() -> None:
    table = _build_table()
    if not table:
        raise SystemExit("No corpora with a topic map / index found. Gather some first.")
    models = {e.model_id for e in table.values()}
    model_id = max(models, key=lambda m: sum(e.model_id == m for e in table.values()))
    print(f"Routing table: {len(table)} corpora — {', '.join(sorted(table))}")
    print(f"Embedding model: {model_id}  (groups present: {sorted(models)})\n")

    pos_top, pos_correct_score, neg_top = [], [], []
    print("=== positives (should route to the labelled corpus) ===")
    for query, expected in POSITIVES.items():
        if expected not in table:
            print(f"  [skip — {expected} not gathered] {query!r}")
            continue
        ranked = _ranked(table, model_id, query)
        top = ranked[0]
        ok = "OK " if top.corpus == expected else "MISS"
        pos_top.append((top.corpus == expected, top.score))
        score_for_expected = next(m.score for m in ranked if m.corpus == expected)
        pos_correct_score.append(score_for_expected)
        print(f"  {ok} {top.corpus:<14} {top.score:.3f}  (want {expected})  {query!r}")

    print("\n=== negatives (should abstain) ===")
    for query in NEGATIVES:
        ranked = _ranked(table, model_id, query)
        top = ranked[0]
        neg_top.append(top.score)
        print(f"  top {top.corpus:<14} {top.score:.3f}  {query!r}")

    rank1 = sum(ok for ok, _ in pos_top) / len(pos_top) if pos_top else 0.0
    print(f"\nrank-1 accuracy on positives: {rank1:.2f}")
    if pos_correct_score:
        print(
            f"expected-corpus score:  median {st.median(pos_correct_score):.3f}  "
            f"min {min(pos_correct_score):.3f}"
        )
    if neg_top:
        print(
            f"off-topic top score:    median {st.median(neg_top):.3f}  "
            f"max {max(neg_top):.3f}"
        )

    print("\n=== floor sweep ===")
    print(f"{'floor':>6}  {'pos kept&correct':>16}  {'pos wrong-conf':>14}  {'neg abstained':>13}")
    best = None
    for floor in FLOORS:
        kept_ok = sum(ok and s >= floor for ok, s in pos_top)
        wrong_conf = sum((not ok) and s >= floor for ok, s in pos_top)
        neg_abst = sum(s < floor for s in neg_top)
        # Maximise correct confident positives + abstained negatives, penalise
        # confident-but-wrong positives.
        score = kept_ok + neg_abst - 2 * wrong_conf
        print(
            f"{floor:>6.2f}  {kept_ok:>7}/{len(pos_top):<8}  {wrong_conf:>14}  "
            f"{neg_abst:>7}/{len(neg_top):<5}"
        )
        if best is None or score > best[1]:
            best = (floor, score)
    if best:
        print(
            f"\nrecommended floor ≈ {best[0]:.2f}  →  "
            f"export IETF_LLM_ROUTING_MIN_SCORE={best[0]:.2f}"
        )
    print(
        "\nReminder: a centroid built from few documents, or heavily "
        "overlapping WGs (tls/cfrg), will blur the boundary — widen the probe "
        "sets and re-run before trusting a single number."
    )


if __name__ == "__main__":
    main()
