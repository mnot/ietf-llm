"""Shared helpers for the gather layer's JSON-dict manifests
(`materials.json`, `documents.json`): a tolerant read and an atomic
write, so each manifest module is just a path plus these two calls.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Mapping


def load_json_dict(path: str) -> Dict[str, str]:
    """Return the `{str: str}` map at `path`, or {} if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json_dict(path: str, data: Mapping[str, str]) -> None:
    """Persist `data` atomically (tmp + rename, so a concurrent reader
    never sees a half-written file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(data), fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
