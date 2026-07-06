"""Per-WG record of which material revision we last fetched.

The Datatracker materials *content* endpoint ignores conditional GET,
but each material is a document with a `rev` in the (cheap, ETag-
cacheable) document API. We store the rev we last wrote for each
material doc and re-fetch its content only when the rev changes — so
revised minutes / agendas get picked up without re-downloading
unchanged ones every gather.

The manifest is machinery, not corpus: it lives at
`~/.cache/ietf-llm/<wg>/materials.json` (beside the embeddings DB and
the last-gathered sentinel), so it is neither indexed nor exported.
It maps `<doc-name> → <rev>`, e.g. `{"minutes-125-httpbis": "01"}`.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping

from ...utils import get_cache_dir
from .json_store import load_json_dict, save_json_dict


def _manifest_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "materials.json")


def load_manifest(wg: str) -> Dict[str, str]:
    """Return the `<doc-name> → rev` map, or {} if absent / unreadable."""
    return load_json_dict(_manifest_path(wg))


def save_manifest(wg: str, manifest: Mapping[str, str]) -> None:
    """Persist the `<doc-name> → rev` map atomically."""
    save_json_dict(_manifest_path(wg), manifest)
