"""Tests for ietf_llm.config — per-WG scoped argument persistence.

Verifies the contract described in config.py:
- scalars: CLI overrides persisted unless CLI value equals declared default
- lists: CLI extends persisted (set union, sorted, deduped)
- clear(): wipes the per-WG config directory
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ietf_llm import config


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _config_file(home: Path, wg: str, scope: str) -> Path:
    return home / ".config" / "ietf-llm" / wg / f"{scope}.json"


def test_save_load_roundtrip(isolated_home: Path) -> None:
    config.save("foo", "gather", {"months": 6, "github": ["a/b"]})
    assert config.load("foo", "gather") == {"months": 6, "github": ["a/b"]}


def test_load_missing_returns_empty(isolated_home: Path) -> None:
    assert config.load("nope", "gather") == {}


def test_load_malformed_returns_empty(isolated_home: Path) -> None:
    path = _config_file(isolated_home, "broken", "gather")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert config.load("broken", "gather") == {}


def test_merge_scalar_cli_overrides_persisted(isolated_home: Path) -> None:
    config.save("wg", "gather", {"months": 6})
    args = _ns(months=12)
    config.merge(args, "wg", "gather", scalars=("months",), lists=())
    assert args.months == 12
    assert config.load("wg", "gather")["months"] == 12


def test_merge_scalar_uses_persisted_when_at_default(isolated_home: Path) -> None:
    config.save("wg", "gather", {"months": 6})
    args = _ns(months=12)  # 12 is the declared default → treat as not supplied
    config.merge(
        args,
        "wg",
        "gather",
        scalars=("months",),
        lists=(),
        defaults={"months": 12},
    )
    assert args.months == 6


def test_merge_scalar_uses_persisted_when_none(isolated_home: Path) -> None:
    config.save("wg", "gather", {"destination": "/old/path"})
    args = _ns(destination=None)
    config.merge(args, "wg", "gather", scalars=("destination",), lists=())
    assert args.destination == "/old/path"


def test_merge_list_unions_cli_with_persisted(isolated_home: Path) -> None:
    config.save("wg", "gather", {"github": ["a/b", "c/d"]})
    args = _ns(github=["e/f", "a/b"])  # 'a/b' overlaps
    config.merge(args, "wg", "gather", scalars=(), lists=("github",))
    assert args.github == ["a/b", "c/d", "e/f"]  # set-union, sorted
    assert config.load("wg", "gather")["github"] == ["a/b", "c/d", "e/f"]


def test_merge_list_handles_legacy_string_form(isolated_home: Path) -> None:
    # Older configs sometimes stored a list as a single string.
    config.save("wg", "gather", {"github": "a/b"})
    args = _ns(github=["c/d"])
    config.merge(args, "wg", "gather", scalars=(), lists=("github",))
    assert args.github == ["a/b", "c/d"]


def test_merge_list_empty_returns_none(isolated_home: Path) -> None:
    args = _ns(github=None)
    config.merge(args, "wg", "gather", scalars=(), lists=("github",))
    assert args.github is None


def test_clear_removes_per_wg_dir(isolated_home: Path) -> None:
    config.save("wg", "gather", {"x": 1})
    config.save("wg", "export", {"y": 2})
    assert _config_file(isolated_home, "wg", "gather").exists()
    assert _config_file(isolated_home, "wg", "export").exists()
    assert config.clear("wg") is True
    assert not _config_file(isolated_home, "wg", "gather").exists()
    assert not _config_file(isolated_home, "wg", "export").exists()


def test_clear_returns_false_when_nothing_to_clear(isolated_home: Path) -> None:
    assert config.clear("never-existed") is False


def test_merge_persists_only_non_default_values(isolated_home: Path) -> None:
    # If the user passes the default, we shouldn't pollute the persisted
    # config with it — that would prevent the default itself from ever
    # changing in a future version.
    args = _ns(months=12)
    config.merge(
        args,
        "wg",
        "gather",
        scalars=("months",),
        lists=(),
        defaults={"months": 12},
    )
    saved = json.loads(_config_file(isolated_home, "wg", "gather").read_text())
    assert "months" not in saved
