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

Like `materials.json`, this is machinery, not corpus: it lives in the
corpus *root* — `~/.cache/ietf-llm/<wg>/documents.json`, beside the
embeddings DB and the last-gathered sentinel rather than under `files/` —
so it is neither indexed nor exported. It maps
`<draft-name> → {"expires": <iso|"">, "state": <slug|None>}`, e.g.
`{"draft-ietf-httpbis-no-vary-search": {"expires": "2026-11-14T05:41:13Z",
"state": "active"}}`.

Sitting in the corpus root means it *is* part of a published version (the
gather workspace is the corpus root, so `publish` picks it up), but on the
cloud backend a version is materialised into per-version scratch, not back
into `<cache>/<wg>/`. Hence the two loaders below: reader-side
(`load_documents_manifest`, resolved through the `CorpusStore` seam) and
gather-side (`load_workspace_manifest`, the local workspace).
They are the same path on the local backend.

The loader tolerates the legacy flat shape (`<name> → <expires-iso>`)
written before state was recorded: a bare string value reads as
`{"expires": <str>, "state": None}`, so a cache gathered under the old
format keeps working until its next gather rewrites the manifest.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Mapping, Optional

from ...atomicio import atomic_open
from ...paths import get_cache_dir
from ...store.corpus import get_corpus_store

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


#: Filename of the manifest inside a corpus root.
_MANIFEST_NAME = "documents.json"


def _workspace_path(wg: str) -> str:
    """The manifest in the *local gather workspace* — `<cache>/<wg>/`, where a
    gather writes it and where the gather-side reader (the embedding build) must
    look, since the tree being indexed is the one on local disk, not the last
    published version."""
    return os.path.join(get_cache_dir(), wg, _MANIFEST_NAME)


def _read_path(wg: str) -> Optional[str]:
    """The manifest for the corpus's *current version*, resolved through the
    `CorpusStore` seam, or None when the corpus has no current version.

    The manifest lives in the corpus root beside `files/`, so it is part of a
    published version — but on the cloud backend that version is materialised
    into per-version scratch, never into `<cache>/<wg>/`. Composing the path from
    `get_cache_dir()` there finds nothing and every reader silently degrades to
    "no lifecycle state recorded", so the read side goes through
    `local_corpus_dir` (identical to the old path on the local backend)."""
    root = get_corpus_store().local_corpus_dir(wg)
    return os.path.join(root, _MANIFEST_NAME) if root else None


def _load(path: Optional[str]) -> Dict[str, DocumentRecord]:
    """Read and normalise a manifest file. Shared by the reader-side and
    workspace loaders — see `load_documents_manifest` for the shapes."""
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
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


def load_documents_manifest(wg: str) -> Dict[str, DocumentRecord]:
    """Return the `<draft-name> → {expires, state}` map, or {} if absent.

    The reader-side loader: resolves the corpus's current version through the
    store seam, so it works on the cloud backend as well as the local one.

    Normalises the legacy flat shape (string value = bare expiry) into the
    record shape so a stale on-disk manifest keeps working before its next
    gather. Unreadable / non-dict files yield {}.

    Not total, unlike the pre-seam version: resolving the version can raise
    `VersionVanished` on the cloud backend (a concurrent publish reaped the
    pinned version). Every caller sits under `_requires_corpus`, which re-runs
    the whole tool call once on a fresh pin — so a caller outside that guard has
    to handle it.
    """
    return _load(_read_path(wg))


def load_workspace_manifest(wg: str) -> Dict[str, DocumentRecord]:
    """The manifest as it stands in the local gather workspace.

    The gather-side counterpart of `load_documents_manifest`: same shapes, but
    read from `<cache>/<wg>/` rather than the current published version. The
    embedding build needs this one — it runs mid-gather, over the workspace the
    gather has just written and has not published yet, so the published
    version's manifest would describe the *previous* set of drafts."""
    return _load(_workspace_path(wg))


def save_documents_manifest(wg: str, manifest: Mapping[str, DocumentRecord]) -> None:
    """Persist the `<draft-name> → {expires, state}` map atomically
    (tmp + rename, so a concurrent reader never sees a half-written file).

    Always writes the local gather workspace — the tree `publish` then uploads —
    never a materialised version, which is immutable."""
    path = _workspace_path(wg)
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

    Reads the *workspace* manifest: this runs on the gather path, over the tree
    the gather has just written and not yet published.
    """
    return {
        name
        for name, rec in load_workspace_manifest(wg).items()
        if rec.get("state") in SKIP_EMBED_STATES
    }
