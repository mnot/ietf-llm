"""Tests for --add-mentioned-drafts (the mentioned-draft derivation)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, List

import pytest

from ietf_llm.gather import sequencer as main_mod
from ietf_llm import config
from ietf_llm.paths import drafts_dir
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir

Q = Verbosity.QUIET


def _seed_drafts(cache: str, *files: str) -> None:
    d = drafts_dir(cache)
    os.makedirs(d, exist_ok=True)
    for f in files:
        with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
            fh.write("x\n")


def test_present_draft_names(tmp_path: Path) -> None:
    cache = str(tmp_path)
    _seed_drafts(cache, "draft-foo-bar-00.txt", "draft-foo-bar-01.txt", "rfc9.txt")
    assert main_mod._present_draft_names(cache) == {"draft-foo-bar"}


def test_mentioned_adds_new_valid_drafts_and_persists(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = get_wg_file_cache_dir("rswg")
    _seed_drafts(cache, "draft-already-here-00.txt")  # present → excluded

    fetched: List[str] = []
    # Stub validation (drop the garbage token) and the fetch.
    monkeypatch.setattr(
        main_mod, "validate_draft_names",
        lambda names, verbose: [n for n in names if n != "draft-trunc-ate"],
    )
    monkeypatch.setattr(
        main_mod, "process_extra_drafts",
        lambda names, dest, verbose: fetched.extend(names) or list(names),
    )

    args = argparse.Namespace(wg="rswg", add_mentioned_drafts=True)
    mentioned = [
        "draft-already-here",   # present → skipped
        "draft-new-thing",      # new, valid → added
        "draft-trunc-ate",      # new, invalid → dropped by validation
    ]
    main_mod._gather_mentioned_drafts(args, cache, mentioned, {}, Q)

    assert fetched == ["draft-new-thing"]
    persisted = config.load("rswg", "gather").get("mentioned_drafts")
    assert persisted == ["draft-new-thing"]


def test_mentioned_is_sticky_no_recheck(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = get_wg_file_cache_dir("rswg")
    calls: List[Any] = []
    monkeypatch.setattr(
        main_mod, "validate_draft_names",
        lambda names, verbose: calls.append(names) or list(names),
    )
    monkeypatch.setattr(
        main_mod, "process_extra_drafts", lambda names, dest, verbose: list(names),
    )
    args = argparse.Namespace(wg="rswg", add_mentioned_drafts=True)
    # Everything mentioned is already in the persisted set → no candidates,
    # so validation is never called (no per-gather re-check / churn).
    main_mod._gather_mentioned_drafts(
        args, cache, ["draft-x", "draft-y"], {"mentioned_drafts": ["draft-x", "draft-y"]}, Q
    )
    assert calls == []
