"""Backend selection, cloud-store construction, and serve-config validation."""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path

import pytest

from ietf_llm.store.corpus import LocalCorpusStore, get_corpus_store
from ietf_llm.store.cloud import CloudCorpusStore, build_cloud_store

_STORE_ENV = (
    "IETF_LLM_STORE_BACKEND",
    "IETF_LLM_STORE_URL",
    "IETF_LLM_SCRATCH_DIR",
)


@pytest.fixture(autouse=True)
def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _STORE_ENV:
        monkeypatch.delenv(var, raising=False)


def _select_cloud(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    monkeypatch.setenv("IETF_LLM_STORE_URL", "s3://test-bucket/prefix")
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(base / "scratch"))
    # Construction makes a boto3 client (no network call); give it a region.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_default_backend_is_local(isolated_home: Path) -> None:
    assert isinstance(get_corpus_store(), LocalCorpusStore)


def test_cloud_backend_selected(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_cloud(isolated_home / "store", monkeypatch)
    assert isinstance(get_corpus_store(), CloudCorpusStore)


def test_cloud_store_is_reused_under_unchanged_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_cloud(isolated_home / "store", monkeypatch)
    assert get_corpus_store() is get_corpus_store()


@pytest.mark.parametrize(
    "var, value",
    [
        ("IETF_LLM_STORE_URL", "s3://other-bucket/prefix"),
        ("IETF_LLM_SCRATCH_DIR", "elsewhere"),
        ("IETF_LLM_RESOLVE_TTL", "0"),
        ("IETF_LLM_RETAIN_VERSIONS", "5"),
        ("IETF_LLM_STORE_ENDPOINT_URL", "http://localhost:9000"),
    ],
)
def test_cloud_store_rebuilt_when_config_changes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    _select_cloud(isolated_home / "store", monkeypatch)
    first = get_corpus_store()
    monkeypatch.setenv(var, value)
    second = get_corpus_store()
    assert second is not first
    assert get_corpus_store() is second


def test_failed_build_does_not_displace_the_cached_store(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_cloud(isolated_home / "store", monkeypatch)
    first = get_corpus_store()
    monkeypatch.delenv("IETF_LLM_SCRATCH_DIR")
    with pytest.raises(ValueError):
        get_corpus_store()
    monkeypatch.setenv("IETF_LLM_SCRATCH_DIR", str(isolated_home / "store" / "scratch"))
    assert get_corpus_store() is first


def test_concurrent_first_calls_build_one_store(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor  # pylint: disable=import-outside-toplevel

    _select_cloud(isolated_home / "store", monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: get_corpus_store(), range(32)))
    assert len({id(s) for s in stores}) == 1


def test_cloud_under_configured_raises(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")  # no db / blob / scratch
    with pytest.raises(ValueError):
        build_cloud_store()


def test_boot_validation_flags_cloud_misconfig(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    errors, _warnings = mcp.serve._serve_config_problems("127.0.0.1")
    assert any("under-configured" in e for e in errors)


def test_boot_validation_unknown_backend(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "bogus")
    errors, _warnings = mcp.serve._serve_config_problems("127.0.0.1")
    assert any("not recognised" in e for e in errors)
