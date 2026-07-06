"""R11: cache and config roots are configurable via the environment so a
deployment can point them at a mounted / synced location, while the local
CLI keeps the ~/.cache and ~/.config defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.paths import get_cache_dir, get_config_dir, get_index_dir


def test_cache_dir_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "corpus"
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(target))
    assert get_cache_dir() == str(target)
    assert target.is_dir()  # created if absent


def test_config_dir_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "cfg"
    monkeypatch.setenv("IETF_LLM_CONFIG_DIR", str(target))
    assert get_config_dir() == str(target)
    assert target.is_dir()


def test_dirs_default_under_home_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("IETF_LLM_CACHE_DIR", raising=False)
    monkeypatch.delenv("IETF_LLM_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert get_cache_dir() == str(tmp_path / ".cache" / "ietf-llm")
    assert get_config_dir() == str(tmp_path / ".config" / "ietf-llm")


def test_empty_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # A blank value is treated as unset, not as the current directory.
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", "   ")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert get_cache_dir() == str(tmp_path / ".cache" / "ietf-llm")


def test_index_dir_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    target = tmp_path / "idx"
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(target))
    assert get_index_dir() == str(target)
    assert target.is_dir()


def test_index_dir_defaults_to_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("IETF_LLM_INDEX_DIR", raising=False)
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(tmp_path / "c"))
    assert get_index_dir() == get_cache_dir()
