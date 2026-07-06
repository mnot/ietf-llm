"""Service-scope configuration: settings that are properties of the tool /
deployment, not of a corpus.

Each non-secret key resolves **env > global config.json > default** — so an
operator can set it via the container environment, bake it into the shared
config file, or mix the two. The cloud store is object-store only: one
`IETF_LLM_STORE_URL` (an `s3://` locator) holds both the immutable version
content and the compare-and-swap control keys. Its credentials are a secret,
read from the standard AWS environment / instance-role chain only, never a file.
This is the single place that reads these knobs; the table below is
authoritative. See `docs/storage.md`.

| key             | env var                    | global config.json | secret |
|-----------------|----------------------------|--------------------|--------|
| store backend   | IETF_LLM_STORE_BACKEND     | store_backend      | no     |
| store URL       | IETF_LLM_STORE_URL         | store_url          | no     |
| scratch dir     | IETF_LLM_SCRATCH_DIR       | scratch_dir        | no     |
| resolve TTL (s) | IETF_LLM_RESOLVE_TTL       | resolve_ttl        | no     |
| gather max inflight | IETF_LLM_GATHER_MAX_INFLIGHT | gather_max_inflight | no |
| retain versions | IETF_LLM_RETAIN_VERSIONS   | retain_versions    | no     |

The S3 endpoint for a non-AWS service (R2, MinIO) is `IETF_LLM_STORE_ENDPOINT_URL`
(env only), read by `store.s3`.

The per-host gather egress caps (`IETF_LLM_HTTP_MAX_PER_HOST`,
`IETF_LLM_HTTP_MAX_DATATRACKER`) are deliberately *not* read here: `http_governor`
owns them and reads them straight from the environment, because it sits below
`config.fs` in the import graph (this module imports `config.fs`, which imports
`utils`, which the governor wraps) and must not depend on it.

This module reads **global** config only, and through the `config.fs` leaf rather
than `config` — because `config`'s per-WG path is now store-routed and the store
backend is selected *here* (`store_backend()`), so importing `config` would be a
cycle (`config` → `config.store` → `config.service`). Global config stays
filesystem-bound for exactly that reason.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from . import fs

#: (environment variable, global-config key) for each non-secret service knob.
STORE_BACKEND: Tuple[str, str] = ("IETF_LLM_STORE_BACKEND", "store_backend")
STORE_URL: Tuple[str, str] = ("IETF_LLM_STORE_URL", "store_url")
SCRATCH_DIR: Tuple[str, str] = ("IETF_LLM_SCRATCH_DIR", "scratch_dir")
RESOLVE_TTL: Tuple[str, str] = ("IETF_LLM_RESOLVE_TTL", "resolve_ttl")
GATHER_MAX_INFLIGHT: Tuple[str, str] = (
    "IETF_LLM_GATHER_MAX_INFLIGHT",
    "gather_max_inflight",
)
RETAIN_VERSIONS: Tuple[str, str] = ("IETF_LLM_RETAIN_VERSIONS", "retain_versions")

#: Default seconds to cache a current-version lookup on the cloud backend.
_DEFAULT_RESOLVE_TTL = 10.0

#: Default number of published versions a cloud publish keeps before reaping the
#: rest (current + previous). Two is the safe floor: a replica re-resolves every
#: `resolve_ttl` (≤10s) and publishes are ≥`gather_min_interval` (6h) apart, so a
#: replica can be at most one version behind — keeping the previous one means
#: every version any replica could still believe is current still exists. Raise
#: it for a paranoid deployment; the floor is 1.
_DEFAULT_RETAIN_VERSIONS = 2

#: Default max concurrent gathers (per host and fleet-wide). Small enough to
#: stay polite to shared upstreams, but >1 so a second client's gather does not
#: wait behind the first. Raise for more throughput, set 1 for strict serial.
_DEFAULT_GATHER_MAX_INFLIGHT = 3


def _resolve(key: Tuple[str, str], default: Optional[str]) -> Optional[str]:
    env_var, cfg_key = key
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip():
        return raw.strip()
    value = fs.load_global().get(cfg_key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def store_backend() -> str:
    """The selected CorpusStore backend: `local` (default) or `cloud`."""
    return _resolve(STORE_BACKEND, "local") or "local"


def store_url() -> Optional[str]:
    """Locator for the cloud object store (None if unset). An `s3://bucket/prefix`
    locator naming the bucket that holds both the version content and the
    control-plane keys. Credentials come from the AWS environment / instance-role
    chain; a non-AWS endpoint (R2, MinIO) is set via `IETF_LLM_STORE_ENDPOINT_URL`."""
    return _resolve(STORE_URL, None)


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


def retain_versions() -> int:
    """Number of published versions a cloud publish keeps before reaping older
    ones (default 2: the current version plus the immediately-previous one).
    Keeping the previous version preserves the never-torn-read guarantee — a
    replica can be at most one version behind, so the version it still believes
    is current must outlive the publish that superseded it. Raise it for a
    deployment that wants more headroom (e.g. forced back-to-back re-gathers);
    invalid or sub-1 values fall back to the default, and 1 is the hard floor
    (the current version is never reaped)."""
    raw = _resolve(RETAIN_VERSIONS, None)
    if raw is None:
        return _DEFAULT_RETAIN_VERSIONS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RETAIN_VERSIONS
    return value if value >= 1 else _DEFAULT_RETAIN_VERSIONS
