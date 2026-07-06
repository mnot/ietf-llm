"""The ConfigStore seam: where a corpus's per-WG config lives.

A sibling of `CorpusStore`, dispatched by the same `IETF_LLM_STORE_BACKEND`
selector but kept separate on purpose:

  - **Layering.** Global config selects the store backend (`config.service`
    reads it), so it is structurally filesystem/env-bound and stays out of any
    store. This seam carries only *per-WG* config, which makes that boundary
    explicit.
  - **Cohesion.** `CorpusStore` is about immutable, versioned *content*
    (materialise / publish / reap); per-WG config is small, mutable,
    last-writer-wins, read directly. Different planes — a separate, three-method
    contract rather than more methods on an already-wide seam.

The cloud backend keeps config as control-plane keys (`corpora/<name>/config/
<scope>`), composing the *same* `KvControlPlane` the corpus store's pointer /
lease / status / access keys live on — so it is one bucket, one control plane,
no extra operator configuration. Writes ride the gather lease the caller already
holds (a plain last-writer-wins put); reads are a plain GET, so the serve path
stays read-only.
"""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Tuple

from . import fs, service

if TYPE_CHECKING:  # annotation only; the runtime import is deferred to the cloud path
    from ..store.control import KvControlPlane

# Process-global, bounded-staleness cache of per-WG config reads on the cloud
# backend: (cache_key, wg, scope) -> (raw payload or None, monotonic expiry).
# Mirrors store.cloud._RESOLVE_CACHE and accepts the same trade-off the
# version pointer already does — a re-gather's new config is visible within the
# TTL, and the writing process refreshes its own entry immediately (write-through
# in save / clear). Off unless a positive resolve TTL is configured. Negative
# results (absent config) are cached too, so a burst of reads for an unconfigured
# corpus collapses to one GET. The raw payload is cached (not the parsed dict),
# so a cache hit still returns a fresh dict — `merge` mutates what `load` returns.
# The local backend never touches this.
_CONFIG_CACHE: Dict[Tuple[str, str, str], Tuple[Optional[str], float]] = {}
_CONFIG_LOCK = threading.Lock()


def _clear_config_cache() -> None:
    """Drop every cached config read (test seam / hard reset)."""
    with _CONFIG_LOCK:
        _CONFIG_CACHE.clear()


def _parse_config(raw: Optional[str]) -> Dict[str, Any]:
    """A control-plane config payload as a fresh dict, or {} if absent/malformed."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


class ConfigStore(ABC):
    """Read / write / clear per-WG config for the selected backend."""

    @abstractmethod
    def load(self, wg: str, scope: str) -> Dict[str, Any]:
        """The persisted config dict for (wg, scope), or {} if absent."""

    @abstractmethod
    def save(self, wg: str, scope: str, data: Mapping[str, Any]) -> None:
        """Persist `data` for (wg, scope)."""

    @abstractmethod
    def clear(self, wg: str) -> bool:
        """Remove all of `wg`'s config (every scope). True if anything existed."""


class LocalConfigStore(ConfigStore):
    """Filesystem backend — today's behaviour, unchanged. Delegates straight to
    `config.fs`, so the laptop CLI is unaffected."""

    def load(self, wg: str, scope: str) -> Dict[str, Any]:
        return fs.load(wg, scope)

    def save(self, wg: str, scope: str, data: Mapping[str, Any]) -> None:
        fs.save(wg, scope, data)

    def clear(self, wg: str) -> bool:
        return fs.clear(wg)


class CloudConfigStore(ConfigStore):
    """Control-plane backend — per-WG config as `corpora/<name>/config/<scope>`
    keys, JSON-encoded, last-writer-wins (the gather holds the corpus lease, so
    the writer is already serialised)."""

    def __init__(
        self,
        control: KvControlPlane,
        *,
        resolve_ttl: float = 0.0,
        cache_key: str = "",
    ) -> None:
        self._control = control
        # Caching is opt-in: off (ttl 0) for direct construction (tests read the
        # control plane live), a short TTL via build_cloud_config_store.
        # `cache_key` scopes the process-global cache to this control plane.
        self._resolve_ttl = resolve_ttl
        self._cache_key = cache_key

    def _cached_get(self, wg: str, scope: str) -> Optional[str]:
        if self._resolve_ttl <= 0:
            return self._control.get_config(wg, scope)
        key = (self._cache_key, wg, scope)
        with _CONFIG_LOCK:
            entry = _CONFIG_CACHE.get(key)
            if entry is not None and entry[1] > time.monotonic():
                return entry[0]
        # Read outside the lock so a slow GET doesn't serialise every reader.
        raw = self._control.get_config(wg, scope)
        with _CONFIG_LOCK:
            _CONFIG_CACHE[key] = (raw, time.monotonic() + self._resolve_ttl)
        return raw

    def _cache_put(self, wg: str, scope: str, raw: Optional[str]) -> None:
        """Write-through, so the writing process serves what it just wrote."""
        if self._resolve_ttl <= 0:
            return
        with _CONFIG_LOCK:
            _CONFIG_CACHE[(self._cache_key, wg, scope)] = (
                raw,
                time.monotonic() + self._resolve_ttl,
            )

    def _invalidate(self, wg: str) -> None:
        if self._resolve_ttl <= 0:
            return
        with _CONFIG_LOCK:
            for key in [
                k for k in _CONFIG_CACHE if k[0] == self._cache_key and k[1] == wg
            ]:
                del _CONFIG_CACHE[key]

    def load(self, wg: str, scope: str) -> Dict[str, Any]:
        return _parse_config(self._cached_get(wg, scope))

    def save(self, wg: str, scope: str, data: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(data), sort_keys=True)
        self._control.set_config(wg, scope, payload)
        self._cache_put(wg, scope, payload)

    def clear(self, wg: str) -> bool:
        removed = bool(self._control.clear_config(wg))
        self._invalidate(wg)
        return removed


def build_cloud_config_store() -> CloudConfigStore:
    """Construct the cloud ConfigStore from service config, or raise ValueError
    if selected but under-configured. Builds its own `KvControlPlane` over the
    one S3 bucket (a cheap, lazily-connecting handle); the keys live under the
    same `corpora/<name>/` prefix as the corpus store's control keys."""
    store_url = service.store_url()
    if not store_url:
        raise ValueError(
            "cloud config store selected but not configured: missing "
            "IETF_LLM_STORE_URL"
        )
    if not store_url.startswith("s3://"):
        raise ValueError(
            "the cloud store is object-store only: IETF_LLM_STORE_URL must be an "
            f"s3:// locator (got {store_url!r})"
        )
    try:
        from ..store.kv_s3 import S3KvStore  # pylint: disable=import-outside-toplevel
        from ..store.s3 import S3Bucket  # pylint: disable=import-outside-toplevel
    except ImportError as err:
        raise ValueError(
            "an s3:// store needs the 's3' extra (pip install ietf-llm[s3])"
        ) from err
    # Deferred on purpose — and load-bearing, not just lazy. This is the only
    # `store` import in the whole `config` package. `store.corpus` imports
    # `config.service`, so importing config already reaches back into store's
    # consumers; hoisting this to module top would close a real cycle:
    #   config.store -> store.control -> store/__init__ -> store.corpus
    #     -> config.service -> config.store
    # Keep it here (and the sibling S3 imports above) function-local. Do not tidy up.
    # pylint: disable-next=import-outside-toplevel
    from ..store.control import KvControlPlane

    return CloudConfigStore(
        KvControlPlane(S3KvStore(S3Bucket(store_url))),
        resolve_ttl=service.resolve_ttl(),
        cache_key=store_url,
    )


def get_config_store() -> ConfigStore:
    """The `ConfigStore` for the configured backend.

    `local` (default) keeps config on the filesystem — today's behaviour. `cloud`
    routes per-WG config through the control plane, so a fleet shares it with no
    `IETF_LLM_CONFIG_DIR` mount. Same backend selector as `get_corpus_store`; an
    unrecognised value raises rather than silently falling back to local."""
    backend = service.store_backend()
    if backend == "local":
        return LocalConfigStore()
    if backend == "cloud":
        return build_cloud_config_store()
    raise ValueError(
        f"unknown IETF_LLM_STORE_BACKEND {backend!r} (expected 'local' or 'cloud')"
    )
