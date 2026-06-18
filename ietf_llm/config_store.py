"""The ConfigStore seam: where a corpus's per-WG config lives.

A sibling of `CorpusStore`, dispatched by the same `IETF_LLM_STORE_BACKEND`
selector but kept separate on purpose:

  - **Layering.** Global config selects the store backend (`service_config`
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
from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping

from . import config_fs, service_config
from .kv_control import KvControlPlane


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
    `config_fs`, so the laptop CLI is unaffected."""

    def load(self, wg: str, scope: str) -> Dict[str, Any]:
        return config_fs.load(wg, scope)

    def save(self, wg: str, scope: str, data: Mapping[str, Any]) -> None:
        config_fs.save(wg, scope, data)

    def clear(self, wg: str) -> bool:
        return config_fs.clear(wg)


class CloudConfigStore(ConfigStore):
    """Control-plane backend — per-WG config as `corpora/<name>/config/<scope>`
    keys, JSON-encoded, last-writer-wins (the gather holds the corpus lease, so
    the writer is already serialised)."""

    def __init__(self, control: KvControlPlane) -> None:
        self._control = control

    def load(self, wg: str, scope: str) -> Dict[str, Any]:
        raw = self._control.get_config(wg, scope)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def save(self, wg: str, scope: str, data: Mapping[str, Any]) -> None:
        self._control.set_config(wg, scope, json.dumps(dict(data), sort_keys=True))

    def clear(self, wg: str) -> bool:
        return bool(self._control.clear_config(wg))


def build_cloud_config_store() -> CloudConfigStore:
    """Construct the cloud ConfigStore from service config, or raise ValueError
    if selected but under-configured. Builds its own `KvControlPlane` over the
    one S3 bucket (a cheap, lazily-connecting handle); the keys live under the
    same `corpora/<name>/` prefix as the corpus store's control keys."""
    store_url = service_config.store_url()
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
        from .kv_store_s3 import S3KvStore  # pylint: disable=import-outside-toplevel
        from .s3_backend import S3Bucket  # pylint: disable=import-outside-toplevel
    except ImportError as err:
        raise ValueError(
            "an s3:// store needs the 's3' extra (pip install ietf-llm[s3])"
        ) from err
    return CloudConfigStore(KvControlPlane(S3KvStore(S3Bucket(store_url))))


def get_config_store() -> ConfigStore:
    """The `ConfigStore` for the configured backend.

    `local` (default) keeps config on the filesystem — today's behaviour. `cloud`
    routes per-WG config through the control plane, so a fleet shares it with no
    `IETF_LLM_CONFIG_DIR` mount. Same backend selector as `get_corpus_store`; an
    unrecognised value raises rather than silently falling back to local."""
    backend = service_config.store_backend()
    if backend == "local":
        return LocalConfigStore()
    if backend == "cloud":
        return build_cloud_config_store()
    raise ValueError(
        f"unknown IETF_LLM_STORE_BACKEND {backend!r} (expected 'local' or 'cloud')"
    )
