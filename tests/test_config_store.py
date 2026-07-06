"""Tests for the ConfigStore seam (local filesystem + cloud control plane).

The local backend is the existing behaviour (covered more broadly in
test_config.py via config.load/save/clear, which now dispatch here). These
focus on the cloud backend and the seam contract — in particular that config
written by one host is visible to another through the shared control plane,
which is what makes cloud `--all` re-gather a corpus correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.config import store as config_store
from ietf_llm.config.store import (
    CloudConfigStore,
    ConfigStore,
    LocalConfigStore,
    get_config_store,
)
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    config_store._clear_config_cache()


def _cloud() -> CloudConfigStore:
    return CloudConfigStore(KvControlPlane(InMemoryKvStore()))


# --- contract (both backends) ---------------------------------------------


def test_local_round_trip(isolated_home: Path) -> None:
    store = LocalConfigStore()
    assert store.load("tls", "gather") == {}
    store.save("tls", "gather", {"mailing_list": ["tls"]})
    assert store.load("tls", "gather") == {"mailing_list": ["tls"]}


def test_cloud_round_trip() -> None:
    store = _cloud()
    assert store.load("tls", "gather") == {}
    store.save("tls", "gather", {"mailing_list": ["tls"]})
    assert store.load("tls", "gather") == {"mailing_list": ["tls"]}


def test_cloud_scopes_are_independent() -> None:
    store = _cloud()
    store.save("tls", "gather", {"a": 1})
    store.save("tls", "export", {"b": 2})
    assert store.load("tls", "gather") == {"a": 1}
    assert store.load("tls", "export") == {"b": 2}


def test_cloud_clear_removes_every_scope() -> None:
    store = _cloud()
    store.save("tls", "gather", {"a": 1})
    store.save("tls", "export", {"b": 2})
    assert store.clear("tls") is True
    assert store.load("tls", "gather") == {}
    assert store.load("tls", "export") == {}
    assert store.clear("tls") is False  # idempotent


def test_cloud_load_tolerates_malformed_payload() -> None:
    control = KvControlPlane(InMemoryKvStore())
    control.set_config("tls", "gather", "not json")
    store = CloudConfigStore(control)
    assert store.load("tls", "gather") == {}


def test_cloud_config_is_visible_across_hosts() -> None:
    # The correctness claim behind cloud `--all`: a corpus gathered on one host
    # has its config readable on another through the shared control plane.
    kv = InMemoryKvStore()
    host_a = CloudConfigStore(KvControlPlane(kv))
    host_b = CloudConfigStore(KvControlPlane(kv))
    host_a.save("x-synthetic", "gather", {"draft": ["draft-foo"]})
    # Host B (running --all) sees A's sources, so it won't re-gather from empty.
    assert host_b.load("x-synthetic", "gather") == {"draft": ["draft-foo"]}


# --- dispatch --------------------------------------------------------------


def test_get_config_store_defaults_local(isolated_home: Path) -> None:
    assert isinstance(get_config_store(), LocalConfigStore)
    assert isinstance(get_config_store(), ConfigStore)


def test_get_config_store_rejects_unknown_backend(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "Cloud")  # typo -> not 'cloud'
    with pytest.raises(ValueError, match="unknown IETF_LLM_STORE_BACKEND"):
        get_config_store()


# --- cloud read cache (TTL, cloud path only) -------------------------------


def _cached_cloud(control: KvControlPlane, ttl: float = 100.0) -> CloudConfigStore:
    return CloudConfigStore(control, resolve_ttl=ttl, cache_key="bucket")


def test_uncached_by_default_sees_backend_writes_immediately() -> None:
    # Direct construction (ttl 0) does not cache: a write straight to the
    # control plane is visible at once. This is what the cross-host and
    # round-trip tests above rely on.
    control = KvControlPlane(InMemoryKvStore())
    store = CloudConfigStore(control)  # ttl 0
    control.set_config("tls", "gather", '{"a": 1}')
    assert store.load("tls", "gather") == {"a": 1}


def test_read_is_cached_within_ttl() -> None:
    control = KvControlPlane(InMemoryKvStore())
    store = _cached_cloud(control)
    assert store.load("tls", "gather") == {}  # caches the absent result
    # A write that bypasses the store is NOT seen while the cache is warm.
    control.set_config("tls", "gather", '{"a": 1}')
    assert store.load("tls", "gather") == {}
    # ... until the cache is dropped (stands in for TTL expiry).
    config_store._clear_config_cache()
    assert store.load("tls", "gather") == {"a": 1}


def test_save_write_through_is_visible_despite_cache() -> None:
    control = KvControlPlane(InMemoryKvStore())
    store = _cached_cloud(control)
    store.load("tls", "gather")  # warm the cache with {}
    store.save("tls", "gather", {"a": 1})
    # The writing process must serve what it just wrote, not the cached {}.
    assert store.load("tls", "gather") == {"a": 1}


def test_clear_invalidates_cache() -> None:
    control = KvControlPlane(InMemoryKvStore())
    store = _cached_cloud(control)
    store.save("tls", "gather", {"a": 1})
    store.load("tls", "gather")  # cache it
    store.clear("tls")
    assert store.load("tls", "gather") == {}


def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(config_store.time, "monotonic", lambda: clock[0])
    control = KvControlPlane(InMemoryKvStore())
    store = _cached_cloud(control, ttl=10.0)
    store.load("tls", "gather")  # caches {}
    control.set_config("tls", "gather", '{"a": 1}')
    clock[0] += 11  # past the TTL
    assert store.load("tls", "gather") == {"a": 1}
