"""Per-WG record of each draft's Datatracker state and expiry.

The overview lists a WG's documents, but the people-digest source it
draws from carries no state — so long-concluded drafts (the RFC 7230-era
`p1`..`p7`, say) read as current. We capture two signals at gather time
so readers don't need a network call:

  - `expires`: an *active* Internet-Draft always carries a future expiry
    while expired / replaced / published ones are in the past. The
    overview uses this to separate live drafts from finished work.
  - `state`: the Datatracker draft-state slug (`active` / `expired` /
    `rfc` / `repl` / …). The embedding build uses it to skip a draft's
    whole revision stack once it's `rfc` (the published RFC is canonical)
    or `repl` (replaced — a renamed/merged lineage whose content lives in
    its successor). Those revisions stay on disk (read / cite / grep);
    only embedding is gated. `SKIP_EMBED_STATES` is the set.

Like `materials.json`, this is machinery, not corpus: it lives at
`~/.cache/ietf-llm/<wg>/documents.json` (beside the embeddings DB and
the last-gathered sentinel), so it is neither indexed nor exported. It
maps `<draft-name> → {"expires": <iso|"">, "state": <slug|None>}`, e.g.
`{"draft-ietf-httpbis-no-vary-search": {"expires": "2026-11-14T05:41:13Z",
"state": "active"}}`.

The loader tolerates the legacy flat shape (`<name> → <expires-iso>`)
written before state was recorded: a bare string value reads as
`{"expires": <str>, "state": None}`, so a cache gathered under the old
format keeps working until its next gather rewrites the manifest.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Mapping, Optional

from ...utils import atomic_open, get_cache_dir

#: A single draft's record: ISO expiry (or "") and Datatracker state slug
#: (or None when unknown — e.g. captured during an API outage).
DocumentRecord = Dict[str, Optional[str]]

#: Draft Datatracker states whose revision stack we do NOT embed. The
#: content is canonical elsewhere — the published RFC (`rfc`) or the draft
#: that replaced this lineage (`repl`) — so the full revision history is
#: noise a semantic index shouldn't carry. `expired` / withdrawn drafts are
#: deliberately left embeddable: they're low-volume and sometimes hold
#: unique never-published content.
SKIP_EMBED_STATES = frozenset({"rfc", "repl"})


def _manifest_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "documents.json")


def load_documents_manifest(wg: str) -> Dict[str, DocumentRecord]:
    """Return the `<draft-name> → {expires, state}` map, or {} if absent.

    Normalises the legacy flat shape (string value = bare expiry) into the
    record shape so a stale on-disk manifest keeps working before its next
    gather. Unreadable / non-dict files yield {}.
    """
    try:
        with open(_manifest_path(wg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, DocumentRecord] = {}
    for name, value in data.items():
        if isinstance(value, str):  # legacy flat shape: name → expires-iso
            out[name] = {"expires": value, "state": None}
        elif isinstance(value, dict):
            out[name] = {
                "expires": value.get("expires") or "",
                "state": value.get("state"),
            }
    return out


def save_documents_manifest(wg: str, manifest: Mapping[str, DocumentRecord]) -> None:
    """Persist the `<draft-name> → {expires, state}` map atomically
    (tmp + rename, so a concurrent reader never sees a half-written file)."""
    path = _manifest_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with atomic_open(path) as fh:
        json.dump(dict(manifest), fh, indent=2, sort_keys=True)


def skip_embed_draft_names(wg: str) -> set[str]:
    """Base draft names whose revision files the embedding index skips.

    A draft is in the set when its recorded Datatracker state is in
    `SKIP_EMBED_STATES`. Names are the version-less Datatracker document
    names (e.g. `draft-ietf-httpbis-semantics`); the eligibility filter
    normalises on-disk revision filenames to match. Empty when no manifest
    exists (a corpus gathered before state was recorded embeds everything
    until its next gather), so the skip is never applied blind.
    """
    return {
        name
        for name, rec in load_documents_manifest(wg).items()
        if rec.get("state") in SKIP_EMBED_STATES
    }
