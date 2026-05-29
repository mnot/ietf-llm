"""
Per-WG persisted configuration, scoped by tool.

Each CLI (`ietf-llm` for gather, `ietf-llm-export` for export) keeps its
own JSON file under ~/.config/ietf-llm/<wg>/ so the two tools don't fight
over the same keys. The on-disk shape per file is just:

    {"<scalar>": <value>, "<list>": [...], ...}

Persistence rules (per scope):
- Scalars: CLI overrides persisted. If CLI value is None or equals the
  declared default, the persisted value (if any) is used. A non-default
  CLI value is written back to disk.
- Lists: CLI extends persisted (set union; idempotent across runs).

A scope's `defaults` mapping is consulted only to decide whether a CLI
value should be treated as "user-supplied" or "argparse default fallback";
it does not itself contribute to the merged result.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .utils import LogLevel, atomic_open, get_config_dir, log


def _config_path(wg: str, scope: str) -> str:
    return os.path.join(get_config_dir(), wg, f"{scope}.json")


def load(wg: str, scope: str) -> Dict[str, Any]:
    """Return the persisted config dict for (wg, scope), or {}."""
    path = _config_path(wg, scope)
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
    path = _config_path(wg, scope)
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


def merge(
    args: argparse.Namespace,
    wg: str,
    scope: str,
    scalars: Iterable[str],
    lists: Iterable[str],
    defaults: Optional[Mapping[str, Any]] = None,
) -> None:
    """Merge persisted config into `args` and persist updated values.

    Mutates `args` in place: any persisted scalar that wasn't set on the
    CLI (or that equals the declared default) is filled in; any persisted
    list is union'd with the CLI list. Then writes the resulting merged
    dict back to disk so future runs see today's CLI values.
    """
    defaults = defaults or {}
    persisted = load(wg, scope)

    for key in scalars:
        val = getattr(args, key, None)
        default = defaults.get(key)
        is_default = val is None or val == default
        if is_default and key in persisted:
            setattr(args, key, persisted[key])
        elif not is_default:
            persisted[key] = val

    for key in lists:
        cli_vals: List[Any] = list(getattr(args, key, None) or [])
        persisted_vals = persisted.get(key, [])
        if isinstance(persisted_vals, str):  # tolerate old single-string form
            persisted_vals = [persisted_vals]
        combined = sorted(set(persisted_vals) | set(cli_vals))
        setattr(args, key, combined if combined else None)
        if combined:
            persisted[key] = combined

    save(wg, scope, persisted)
