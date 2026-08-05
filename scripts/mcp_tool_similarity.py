#!/usr/bin/env python
"""Find tool descriptions a model can't tell apart, and probe which tool a
query actually selects.

The budget gate caps how big the surface may get; `mcp_surface_report.py` says
where the weight is. Neither says whether the surface still *works* — and the
failure mode of trimming a docstring is not that it gets too short, it is that
two tools stop being distinguishable and the client picks the wrong one. This
measures that directly.

Needs the real embedding model, so it does not run in CI (the serve venv and CI
are torch-free on purpose). Same deal as `calibrate_routing.py`: run it from an
environment that has the model.

    .venv/bin/python scripts/mcp_tool_similarity.py
    .venv/bin/python scripts/mcp_tool_similarity.py --probes --top 20

Two things are reported:

  pairs   — the most similar tool descriptions. A high-scoring pair is a
            routing hazard: whatever distinguishes them is not in the text the
            client reads. Fix by sharpening the *first* sentence of each, not
            by adding paragraphs to both.

  probes  — hand-written queries with the tool whose opening should anchor
            them. Edit PROBES below.

            Read this one carefully: it is NOT a prediction of what Claude
            picks. A client reads every description in full and reasons over
            them; this is cosine over a small bi-encoder's view of one
            paragraph. What a miss tells you is that the tool's opening
            sentence is a weak anchor for how a user would phrase the need —
            worth knowing, and worth fixing by rewriting that sentence, but a
            miss is not a defect and the pass rate is not a quality score.
            Never wire it into CI. Watch it move across a rewrite; don't read
            the absolute number.

Scores are cosine over mean-centered embeddings. Centering matters here: every
description is IETF-corpus tooling, so the shared component dominates raw
cosine and everything looks similar to everything. Subtracting the mean leaves
what actually separates one tool from another. Only the ranking is meaningful
— the absolute numbers move with the model.

Embeds the tool name plus the description's first paragraph, not the whole
docstring. That is deliberate, not a truncation artefact: the opening is what a
routing decision keys on, and the model's context window would silently cut the
long ones anyway (`start_gather` alone is ~2k tokens).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# pylint: disable=wrong-import-position
from ietf_llm.embeddings import DEFAULT_EMBED_MODEL, _get_embed_model
from ietf_llm.log import Verbosity
from ietf_llm.mcp.server import _quiet_embedding_stack_output
from ietf_llm.mcp.surface import build_surface

# --- EDIT ME -------------------------------------------------------------- #

#: Queries phrased the way a client's internal monologue would phrase them,
#: with the tool that should win. Hand-written on purpose: a probe generated
#: from the docstring only proves the docstring matches itself.
PROBES: Dict[str, str] = {
    "which working group should I look in for this topic": "which_corpus",
    "what is this working group working on right now": "overview",
    "find messages discussing post-quantum key exchange": "search_corpus",
    "search across every corpus I have gathered": "search_corpora",
    "list the open issues on the repository": "read_digest",
    "what happened at the last meeting": "read_minutes",
    "when is this working group meeting next": "meeting_schedule",
    "get the full text of RFC 9110": "get_rfc",
    "who wrote this Internet-Draft": "draft_authors",
    "what is the current state of this draft in the process": "draft_status",
    "show me the drafts this group has adopted": "list_drafts",
    "read the next part of a file I already found": "read_file_section",
    "which messages cite this draft": "find_citations",
    "who replied to this message": "find_replies",
    "add a new working group to my local corpus": "start_gather",
    "is the gather I kicked off finished yet": "gather_status",
    "did the group reach consensus on this proposal": "read_ietf_interpretation_norms",
    "help me write a reply to the mailing list": "read_ietf_participation_norms",
    "count who supported and who objected in a thread": "tally_positions",
    "what has been said about this topic over time": "read_topic",
}

# -------------------------------------------------------------------------- #


def _summary(description: str) -> str:
    """The description's first paragraph — the part a routing decision reads."""
    for block in description.strip().split("\n\n"):
        text = " ".join(block.split())
        if text:
            return text
    return ""


def _embed(model: object, texts: Sequence[str]) -> "np.ndarray":
    vectors = np.array(
        list(model.embed_multi(list(texts))), dtype=np.float32  # type: ignore[attr-defined]
    )
    return vectors


def _centered_unit(vectors: "np.ndarray", mean: "np.ndarray") -> "np.ndarray":
    out = vectors - mean
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.maximum(norms, 1e-9)


def _report_pairs(names: List[str], unit: "np.ndarray", top: int) -> None:
    scores = unit @ unit.T
    pairs: List[Tuple[float, str, str]] = []
    for i, left in enumerate(names):
        for j in range(i + 1, len(names)):
            pairs.append((float(scores[i, j]), left, names[j]))
    pairs.sort(reverse=True)
    print(f"\n=== most confusable tool pairs (top {top}) ===\n")
    for score, left, right in pairs[:top]:
        print(f"  {score:+.3f}  {left}  <->  {right}")


def _report_probes(
    names: List[str], unit: "np.ndarray", mean: "np.ndarray", model: object, top: int
) -> int:
    queries = list(PROBES)
    q_unit = _centered_unit(_embed(model, queries), mean)
    scores = q_unit @ unit.T
    index = {name: i for i, name in enumerate(names)}

    misses = 0
    print("\n=== probe anchoring (NOT a prediction of what a client picks) ===\n")
    for row, query in enumerate(queries):
        want = PROBES[query]
        order = np.argsort(-scores[row])
        winner = names[int(order[0])]
        if want not in index:
            print(f"  ????  {query!r}: expected tool {want!r} is not registered")
            misses += 1
            continue
        rank = int(np.where(order == index[want])[0][0]) + 1
        if rank == 1:
            print(f"  ok    {query[:56]:56} {scores[row][index[want]]:+.3f}")
            continue
        misses += 1
        print(f"  MISS  {query[:56]:56} want {want} (rank {rank}), got {winner}")
    print(f"\n  {len(queries) - misses}/{len(queries)} probes anchor on the tool")
    print(
        "  Compare this against the same run before your edit. A low number on\n"
        "  its own says more about a small bi-encoder than about the surface."
    )
    return misses


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--shape", default="stdio")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--probes", action="store_true", help="also run PROBES")
    args = parser.parse_args(argv)

    _quiet_embedding_stack_output()
    model = _get_embed_model(args.model, Verbosity.QUIET)
    if model is None:
        print(
            f"could not load {args.model}. This script needs the real embedding "
            "model — run it from an environment with the local-embeddings extra, "
            "or point --model at a remote openai-embed/ endpoint.",
            file=sys.stderr,
        )
        return 1

    tools = build_surface(args.shape)
    names = [t.name for t in tools]
    texts = [f"{t.name}: {_summary(t.description)}" for t in tools]
    vectors = _embed(model, texts)
    mean = vectors.mean(axis=0)
    unit = _centered_unit(vectors, mean)

    print(f"{len(names)} tools ({args.shape}), model {args.model}")
    _report_pairs(names, unit, args.top)
    if args.probes:
        # Always exit 0, including on misses: a non-zero status would invite
        # someone to wire this into CI, and the probe result is a diagnostic
        # to read, not a threshold to pass.
        _report_probes(names, unit, mean, model, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
