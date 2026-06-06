"""Service-scope configuration: settings that are properties of the tool /
deployment, not of a corpus.

Each non-secret key resolves **env > global config.json > default** — so an
operator can set it via the container environment, bake it into the shared
config file, or mix the two. Secrets come from the environment only and are
never read from a file (the file:// + SQLite slice has none yet — an S3 access
key / SQL password would be env-only). This is the single place that reads these
knobs; the table below is authoritative. See `docs/storage.md`.

| key             | env var                    | global config.json | secret |
|-----------------|----------------------------|--------------------|--------|
| store backend   | IETF_LLM_STORE_BACKEND     | store_backend      | no     |
| control DB      | IETF_LLM_CONTROL_DB        | control_db         | no     |
| control DB token| IETF_LLM_CONTROL_DB_TOKEN  | —                  | YES    |
| blob dir        | IETF_LLM_BLOB_DIR          | blob_dir           | no     |
| scratch dir     | IETF_LLM_SCRATCH_DIR       | scratch_dir        | no     |
| resolve TTL (s) | IETF_LLM_RESOLVE_TTL       | resolve_ttl        | no     |
| gather max inflight | IETF_LLM_GATHER_MAX_INFLIGHT | gather_max_inflight | no |

The per-host gather egress caps (`IETF_LLM_HTTP_MAX_PER_HOST`,
`IETF_LLM_HTTP_MAX_DATATRACKER`) are deliberately *not* read here: `http_governor`
owns them and reads them straight from the environment, because it sits below
`config` in the import graph (this module imports `config`, which imports
`utils`, which the governor wraps) and must not depend on it.
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
RESOLVE_TTL: Tuple[str, str] = ("IETF_LLM_RESOLVE_TTL", "resolve_ttl")
GATHER_MAX_INFLIGHT: Tuple[str, str] = (
    "IETF_LLM_GATHER_MAX_INFLIGHT",
    "gather_max_inflight",
)

#: Default seconds to cache a current-version lookup on the cloud backend.
_DEFAULT_RESOLVE_TTL = 10.0

#: Default max concurrent gathers (per host and fleet-wide). Small enough to
#: stay polite to shared upstreams, but >1 so a second client's gather does not
#: wait behind the first. Raise for more throughput, set 1 for strict serial.
_DEFAULT_GATHER_MAX_INFLIGHT = 3


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
    """Locator for the control-plane database, or None if unset.

    A filesystem path selects the local SQLite backend (created on first use); a
    cloud database-API locator (e.g. Cloudflare D1) selects that adapter."""
    return _resolve(CONTROL_DB, None)


def control_db_token() -> Optional[str]:
    """API token for a cloud control-plane database (e.g. a Cloudflare D1 token),
    or None if unset. A **secret**: read from the environment only, never the
    config file."""
    raw = os.environ.get("IETF_LLM_CONTROL_DB_TOKEN")
    return raw.strip() if raw and raw.strip() else None


def blob_dir() -> Optional[str]:
    """Base location of the cloud blob store (None if unset)."""
    return _resolve(BLOB_DIR, None)


def scratch_dir() -> Optional[str]:
    """Local directory where the cloud backend materialises versions."""
    return _resolve(SCRATCH_DIR, None)


def resolve_ttl() -> float:
    """Seconds to cache a corpus's current-version lookup on the cloud backend,
    so a burst of reads coalesces to one control-plane call (default 10; 0
    disables). A new version becomes visible fleet-wide within this many seconds
    of a publish — the publishing process refreshes its own cache immediately,
    and since versions are immutable a stale read just serves a valid older
    version. Invalid or negative values fall back to the default."""
    raw = _resolve(RESOLVE_TTL, None)
    if raw is None:
        return _DEFAULT_RESOLVE_TTL
    try:
        ttl = float(raw)
    except ValueError:
        return _DEFAULT_RESOLVE_TTL
    return ttl if ttl >= 0 else _DEFAULT_RESOLVE_TTL


def gather_max_inflight() -> int:
    """Maximum gathers running concurrently (default 3). It bounds both the
    per-host worker pool and the fleet-wide slot count, so a single host runs up
    to this many at once and the whole deployment never exceeds it either. Keeps
    aggregate load on shared upstreams (datatracker, mailarchive, GitHub)
    bounded while letting a second client's gather start without waiting. On the
    local backend only the per-host pool applies (no control-plane slots).
    Invalid or sub-1 values fall back to the default."""
    raw = _resolve(GATHER_MAX_INFLIGHT, None)
    if raw is None:
        return _DEFAULT_GATHER_MAX_INFLIGHT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_GATHER_MAX_INFLIGHT
    return value if value >= 1 else _DEFAULT_GATHER_MAX_INFLIGHT
