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

import json
import os
from typing import Dict

from ..utils import get_cache_dir


def _manifest_path(wg: str) -> str:
    return os.path.join(get_cache_dir(), wg, "materials.json")


def load_manifest(wg: str) -> Dict[str, str]:
    """Return the `<doc-name> → rev` map, or {} if absent / unreadable."""
    try:
        with open(_manifest_path(wg), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(wg: str, manifest: Dict[str, str]) -> None:
    """Persist the manifest atomically (tmp + rename, so a concurrent
    reader never sees a half-written file)."""
    path = _manifest_path(wg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
