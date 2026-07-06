"""Filesystem primitives for persisted configuration.

A leaf module (imports only `utils`) holding the on-disk read/write of per-WG
config (`~/.config/ietf-llm/<wg>/<scope>.json`) and the global service config
(`~/.config/ietf-llm/config.json`). It is the local backend of the `ConfigStore`
seam (`config/store.py`) and the *only* home of global config.

Why split out of `config.py`: per-WG config is dispatched through a store
(`config.load/save/clear` → `get_config_store()`), and the cloud store is chosen
by `config.service.store_backend()`, which reads global config. Keeping these
filesystem primitives here — depended on by `config.service` and the local store
but importing neither — keeps the graph one-directional, the same shape
`freshness.py` has. Global config in particular *must* stay filesystem/env-bound:
it selects the backend, so it can't route through a store chosen by itself.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, Mapping

from ..atomicio import atomic_open
from ..paths import get_config_dir
from ..utils import LogLevel, log


def config_path(wg: str, scope: str) -> str:
    return os.path.join(get_config_dir(), wg, f"{scope}.json")


def load(wg: str, scope: str) -> Dict[str, Any]:
    """Return the persisted config dict for (wg, scope), or {}."""
    path = config_path(wg, scope)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return {}


def save(wg: str, scope: str, data: Mapping[str, Any]) -> None:
    """Persist `data` for (wg, scope)."""
    wg_dir = os.path.join(get_config_dir(), wg)
    os.makedirs(wg_dir, exist_ok=True)
    path = config_path(wg, scope)
    try:
        with atomic_open(path) as fh:
            json.dump(dict(data), fh, indent=2, sort_keys=True)
    except OSError as err:
        log(f"Error saving config ({path}): {err}", level=LogLevel.ERROR)


def clear(wg: str) -> bool:
    """Remove the entire per-WG config directory. Returns True if removed."""
    wg_dir = os.path.join(get_config_dir(), wg)
    if os.path.exists(wg_dir):
        shutil.rmtree(wg_dir)
        return True
    return False


def _global_config_path() -> str:
    return os.path.join(get_config_dir(), "config.json")


def load_global() -> Dict[str, Any]:
    """Return the persisted global (non-WG) service config, or {}.

    Holds settings that are properties of the tool / deployment rather than
    of a corpus (the embedding model, summariser model, embed on/off),
    so they are configured once and apply to every corpus. Always filesystem
    (env-overridable via `config.service`) — never store-routed, since it is
    what selects the store backend.
    """
    path = _global_config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return {}


def save_global(data: Mapping[str, Any]) -> None:
    """Persist the global service config."""
    os.makedirs(get_config_dir(), exist_ok=True)
    path = _global_config_path()
    try:
        with atomic_open(path) as fh:
            json.dump(dict(data), fh, indent=2, sort_keys=True)
    except OSError as err:
        log(f"Error saving global config ({path}): {err}", level=LogLevel.ERROR)
