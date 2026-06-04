"""Tests for the concurrency / correctness hardening from the PR #68 review
(findings G-1..G-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import corpus_control
from ietf_llm.corpus_control import SqliteControlPlane
from ietf_llm.corpus_store import LocalCorpusStore, get_corpus_store
from ietf_llm.gather_runner import _owner

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


# G-9: an unrecognised backend raises rather than silently using Local.
def test_unrecognised_backend_raises(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "Cloud")  # wrong case
    with pytest.raises(ValueError):
        get_corpus_store()


def test_default_backend_is_still_local(isolated_home: Path) -> None:
    assert isinstance(get_corpus_store(), LocalCorpusStore)


# G-10: the lease owner id carries a per-process nonce (host:pid:nonce).
def test_owner_has_per_process_nonce() -> None:
    parts = _owner().split(":")
    assert len(parts) == 3 and all(parts)


# G-3: the SQLite schema is ensured at most once per process per db path.
def test_sqlite_schema_ensured_once(tmp_path: Path) -> None:
    path = str(tmp_path / "c.db")
    corpus_control._sqlite_schema_ensured.discard(path)
    SqliteControlPlane(path)
    assert path in corpus_control._sqlite_schema_ensured
    # A second construction is a no-op for ensure_schema (still works).
    SqliteControlPlane(path).resolve_current("nope")
