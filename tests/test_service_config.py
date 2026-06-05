"""Service-scope config resolution: env > global config.json > default."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import config, service_config

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_CONTROL_DB",
    "IETF_LLM_BLOB_DIR",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


def test_store_backend_default(isolated_home: Path) -> None:
    assert service_config.store_backend() == "local"


def test_store_backend_from_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    assert service_config.store_backend() == "cloud"


def test_store_backend_from_global_config(isolated_home: Path) -> None:
    config.save_global({"store_backend": "cloud"})
    assert service_config.store_backend() == "cloud"


def test_env_beats_global_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.save_global({"store_backend": "cloud"})
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "local")
    assert service_config.store_backend() == "local"


def test_path_keys_resolve_env_then_global_then_none(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.save_global({"control_db": "/cfg/c.db", "blob_dir": "/cfg/bucket"})
    monkeypatch.setenv("IETF_LLM_CONTROL_DB", "/env/c.db")
    assert service_config.control_db() == "/env/c.db"  # env wins
    assert service_config.blob_dir() == "/cfg/bucket"  # global used when env unset
    assert service_config.scratch_dir() is None  # unset everywhere


def test_gather_max_inflight_default(isolated_home: Path) -> None:
    assert service_config.gather_max_inflight() == 3


def test_gather_max_inflight_from_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", "4")
    assert service_config.gather_max_inflight() == 4


def test_gather_max_inflight_invalid_falls_back(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in ("0", "-2", "nope"):
        monkeypatch.setenv("IETF_LLM_GATHER_MAX_INFLIGHT", bad)
        assert service_config.gather_max_inflight() == 3


def test_gather_max_inflight_from_global_config(isolated_home: Path) -> None:
    config.save_global({"gather_max_inflight": "5"})
    assert service_config.gather_max_inflight() == 5
