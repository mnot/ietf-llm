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
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..utils import LogLevel, log
from . import fs
from .store import get_config_store

# Per-WG config (load / save / clear) is dispatched through the ConfigStore seam,
# so a cloud deployment shares it fleet-wide via the control plane with no
# IETF_LLM_CONFIG_DIR mount; the local backend keeps today's filesystem
# behaviour. Global config is *not* store-routed — it selects the backend — so
# load_global / save_global go straight to the filesystem leaf. See
# config/store.py / config/fs.py.


def load(wg: str, scope: str) -> Dict[str, Any]:
    """Return the persisted config dict for (wg, scope), or {}."""
    return get_config_store().load(wg, scope)


def save(wg: str, scope: str, data: Mapping[str, Any]) -> None:
    """Persist `data` for (wg, scope)."""
    get_config_store().save(wg, scope, data)


def clear(wg: str) -> bool:
    """Remove all of `wg`'s persisted config (every scope). Returns True if
    anything was removed."""
    return get_config_store().clear(wg)


def load_global() -> Dict[str, Any]:
    """The persisted global (non-WG) service config, or {}.

    Settings that are properties of the tool / deployment rather than a corpus
    (embedding model, summariser model, embed on/off), configured once and
    applied everywhere. Always filesystem (env-overridable via `config.service`),
    never store-routed — it is what selects the store backend.
    """
    return fs.load_global()


def save_global(data: Mapping[str, Any]) -> None:
    """Persist the global service config (filesystem only)."""
    fs.save_global(data)


def _coerce_env(raw: str, default: Any) -> Any:
    """Coerce an environment string to the type of `default` (bool or str)."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return raw.strip()


def merge_global(
    args: argparse.Namespace,
    spec: Iterable["tuple[str, str, Any]"],
) -> None:
    """Resolve global service scalars into `args`.

    `spec` is an iterable of ``(arg_name, env_var, default)``. Precedence is
    **env > CLI > global-persisted > default**. A CLI value is written
    through to the global config so it sticks for every corpus (even when
    the environment overrides it this run, so it is remembered once the env
    var is gone). The environment wins even over an explicit CLI flag -- a
    container's injected config is authoritative -- and a notice is logged
    when it overrides one. Secrets come from the environment only and are
    never persisted here.

    "Supplied on the CLI" means the arg is not ``None``: callers use
    ``None`` as the unset sentinel (so a flag like ``--embed`` can carry an
    explicit ``False`` that still overrides and persists, distinct from the
    declared default). The resolved source of each scalar (``env`` / ``cli``
    / ``config`` / ``default``) is recorded on ``args._global_sources`` for
    callers that want to explain where a value came from.
    """
    persisted = load_global()
    sources: Dict[str, str] = {}
    for name, env_var, default in spec:
        cli_val = getattr(args, name, None)
        cli_supplied = cli_val is not None
        if cli_supplied:
            persisted[name] = cli_val
        env_raw = os.environ.get(env_var)
        if env_raw is not None and env_raw.strip():
            env_val = _coerce_env(env_raw, default)
            if cli_supplied and cli_val != env_val:
                log(
                    f"Ignoring --{name.replace('_', '-')}={cli_val}; "
                    f"{env_var} is set in the environment and takes precedence.",
                    level=LogLevel.STATUS,
                )
            setattr(args, name, env_val)
            sources[name] = "env"
        elif cli_supplied:
            setattr(args, name, cli_val)
            sources[name] = "cli"
        elif name in persisted:
            setattr(args, name, persisted[name])
            sources[name] = "config"
        else:
            setattr(args, name, default)
            sources[name] = "default"
    save_global(persisted)
    setattr(args, "_global_sources", sources)


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
