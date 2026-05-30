"""Tests for corpus-shape inference (_resolve_corpus_shape): a name is
classified as a Datatracker group, a synthetic (x-) corpus, an inferred
mailing-list corpus, or rejected as a typo — no --list-only flag.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm import __main__ as main_mod
from ietf_llm.utils import Verbosity


def _args(wg: str, **kw: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "wg": wg,
        "mailing_list": None,
        "draft": None,
        "github": None,
        "new_drafts": False,
        "author": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_group: bool,
    is_list: bool,
) -> None:
    monkeypatch.setattr(
        main_mod, "fetch_group_object", lambda wg: {"id": 1} if is_group else None
    )
    monkeypatch.setattr(
        main_mod,
        "validate_list_names",
        lambda names, verbose: list(names) if is_list else [],
    )


def test_group_name_is_group_backed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_group=True, is_list=False)
    args = _args("httpbis")
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, True)


def test_synthetic_name_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # x- short-circuits before any group/list lookup.
    monkeypatch.setattr(
        main_mod, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("x-webbotauth")
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (True, False)


def test_non_group_name_that_is_a_list_becomes_list_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, is_group=False, is_list=True)
    args = _args("last-call")
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list == ["last-call"]  # defaulted to the name


def test_typo_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("htpbis")
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) is None


def test_explicit_sources_make_a_custom_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-group name with explicit --draft is a deliberate custom
    # corpus; the name is not validated as a list and not rejected.
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("mywatch", draft=["draft-foo-bar"])
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None  # untouched


def test_new_drafts_flag_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A generative flag short-circuits to a custom corpus before any
    # group/list lookup, and doesn't default the list to the name.
    monkeypatch.setattr(
        main_mod, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("new-ids", new_drafts=True)
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None


def test_author_flag_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_mod, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("mnot", author="Mark Nottingham")
    assert main_mod._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None


def test_persisted_sources_count_on_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bare re-run of a custom corpus: no CLI sources, but persisted ones
    # exist → not rejected, list not re-defaulted.
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("mywatch")
    persisted: Dict[str, Any] = {"mailing_list": ["somelist"]}
    assert main_mod._resolve_corpus_shape(
        args, persisted, Verbosity.QUIET
    ) == (False, False)
