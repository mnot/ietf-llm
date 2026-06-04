"""Service-scope configuration: settings that are properties of the tool /
deployment, not of a corpus.

Each non-secret key resolves **env > global config.json > default** — so an
operator can set it via the container environment, bake it into the shared
config file, or mix the two. Secrets come from the environment only and are
never read from a file (the file:// + SQLite slice has none yet — an S3 access
key / SQL password would be env-only). This is the single place that reads these
knobs; the table below is authoritative. See `docs/cloud-storage.md`.

| key            | env var                  | global config.json | secret |
|----------------|--------------------------|--------------------|--------|
| store backend  | IETF_LLM_STORE_BACKEND   | store_backend      | no     |
| control DB     | IETF_LLM_CONTROL_DB      | control_db         | no     |
| blob dir       | IETF_LLM_BLOB_DIR        | blob_dir           | no     |
| scratch dir    | IETF_LLM_SCRATCH_DIR     | scratch_dir        | no     |
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from . import config

#: (environment variable, global-config key) for each non-secret service knob.
STORE_BACKEND: Tuple[str, str] = ("IETF_LLM_STORE_BACKEND", "store_backend")
CONTROL_DB: Tuple[str, str] = ("IETF_LLM_CONTROL_DB", "control_db")
BLOB_DIR: Tuple[str, str] = ("IETF_LLM_BLOB_DIR", "blob_dir")
SCRATCH_DIR: Tuple[str, str] = ("IETF_LLM_SCRATCH_DIR", "scratch_dir")


def _resolve(key: Tuple[str, str], default: Optional[str]) -> Optional[str]:
    env_var, cfg_key = key
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip():
        return raw.strip()
    value = config.load_global().get(cfg_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def store_backend() -> str:
    """The selected CorpusStore backend: `local` (default) or `cloud`."""
    return _resolve(STORE_BACKEND, "local") or "local"


def control_db() -> Optional[str]:
    """Path / DSN of the cloud control-plane database (None if unset)."""
    return _resolve(CONTROL_DB, None)


def blob_dir() -> Optional[str]:
    """Base location of the cloud blob store (None if unset)."""
    return _resolve(BLOB_DIR, None)


def scratch_dir() -> Optional[str]:
    """Local directory where the cloud backend materialises versions."""
    return _resolve(SCRATCH_DIR, None)
