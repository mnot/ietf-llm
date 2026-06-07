"""Service-scope config resolution: env > global config.json > default."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import config, service_config

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_STORE_URL",
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
    config.save_global({"store_url": "s3://cfg-bucket", "scratch_dir": "/cfg/scr"})
    monkeypatch.setenv("IETF_LLM_STORE_URL", "s3://env-bucket")
    assert service_config.store_url() == "s3://env-bucket"  # env wins
    assert service_config.scratch_dir() == "/cfg/scr"  # global used when env unset


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
