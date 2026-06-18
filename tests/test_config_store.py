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

from ietf_llm.config_store import (
    CloudConfigStore,
    ConfigStore,
    LocalConfigStore,
    get_config_store,
)
from ietf_llm.kv_control import KvControlPlane
from ietf_llm.kv_store import InMemoryKvStore


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
