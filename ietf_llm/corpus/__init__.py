"""Corpus resolution: identity, canonicalisation, and semantic routing.

Three cohesive-by-domain readers/helpers that answer "which corpus, and what
is it?" — grouped here so the top-level tree isn't littered with them and so
`corpus` no longer collides by name with `store/corpus.py` (the storage
backend). They keep distinct dependency profiles and sit on different sides of
the read/write line, so they stay as separate submodules:

  identity.py    — a corpus's kind/status and one-line description, for the
                   CLI `--list` table and the MCP overview (read side)
  canonical.py   — steer a *new* gather toward an existing overlapping corpus
                   instead of duplicating it (gather / write side)
  routing.py     — per-corpus centroid routing (`which_corpus`) + the fleet
                   routing-table CAS (read side + control plane)

Only the light identity helpers are re-exported here, so `from .. import
corpus; corpus.describe(...)` and `from ..corpus import kind_status` keep
working with the same import surface as before. `routing` / `canonical` are
reached as submodules (`from ..corpus.routing import route`) so their heavier
deps (numpy, embeddings) are not pulled into the identity path.
"""

from __future__ import annotations

from .identity import describe, kind_status, status_cell

__all__ = ["describe", "kind_status", "status_cell"]
