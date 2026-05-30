"""Per-WG record of each draft's Datatracker expiry.

The overview lists a WG's documents, but the people-digest source it
draws from carries no state — so long-concluded drafts (the RFC 7230-era
`p1`..`p7`, say) read as current. The document API returns an `expires`
timestamp per draft, and an *active* Internet-Draft always carries a
future expiry while expired / replaced / published ones are in the past.
We capture that at gather time so the overview can separate live drafts
from finished work without a network call.

Like `materials.json`, this is machinery, not corpus: it lives at
`~/.cache/ietf-llm/<wg>/documents.json` (beside the embeddings DB and
the last-gathered sentinel), so it is neither indexed nor exported. It
maps `<draft-name> → <expires-iso>`, e.g.
`{"draft-ietf-httpbis-no-vary-search": "2026-11-14T05:41:13Z"}`.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping

from ..utils import get_cache_dir
from .json_store import load_json_dict, save_json_dict


def _manifest_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "documents.json")


def load_documents_manifest(wg: str) -> Dict[str, str]:
    """Return the `<draft-name> → expires-iso` map, or {} if absent."""
    return load_json_dict(_manifest_path(wg))


def save_documents_manifest(wg: str, manifest: Mapping[str, str]) -> None:
    """Persist the `<draft-name> → expires-iso` map atomically."""
    save_json_dict(_manifest_path(wg), manifest)
