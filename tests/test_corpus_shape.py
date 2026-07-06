"""Tests for corpus-shape inference (_resolve_corpus_shape): a name is
classified as a Datatracker group, a synthetic (x-) corpus, an inferred
mailing-list corpus, or rejected as a typo — no --list-only flag.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.cli import main as main_mod
from ietf_llm.gather import sequencer
from ietf_llm.log import Verbosity


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
        sequencer, "fetch_group_object", lambda wg: {"id": 1} if is_group else None
    )
    monkeypatch.setattr(
        sequencer,
        "validate_list_names",
        lambda names, verbose: list(names) if is_list else [],
    )


def test_group_name_is_group_backed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_group=True, is_list=False)
    args = _args("httpbis")
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, True)


def test_synthetic_name_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # x- short-circuits before any group/list lookup.
    monkeypatch.setattr(
        sequencer, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("x-webbotauth")
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (True, False)


def test_non_group_name_that_is_a_list_becomes_list_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, is_group=False, is_list=True)
    args = _args("last-call")
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list == ["last-call"]  # defaulted to the name


def test_typo_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("htpbis")
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) is None


def test_explicit_sources_make_a_custom_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-group name with explicit --draft is a deliberate custom
    # corpus; the name is not validated as a list and not rejected.
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("mywatch", draft=["draft-foo-bar"])
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None  # untouched


def test_new_drafts_flag_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A generative flag short-circuits to a custom corpus before any
    # group/list lookup, and doesn't default the list to the name.
    monkeypatch.setattr(
        sequencer, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("new-ids", new_drafts=True)
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None


def test_author_flag_is_custom_no_group_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sequencer, "fetch_group_object",
        lambda wg: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    args = _args("mnot", author="Mark Nottingham")
    assert sequencer._resolve_corpus_shape(args, {}, Verbosity.QUIET) == (False, False)
    assert args.mailing_list is None


def test_persisted_sources_count_on_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bare re-run of a custom corpus: no CLI sources, but persisted ones
    # exist → not rejected, list not re-defaulted.
    _patch(monkeypatch, is_group=False, is_list=False)
    args = _args("mywatch")
    persisted: Dict[str, Any] = {"mailing_list": ["somelist"]}
    assert sequencer._resolve_corpus_shape(
        args, persisted, Verbosity.QUIET
    ) == (False, False)


def test_all_isolates_args_per_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: `--all` must give each corpus its own args object. _gather_one
    # -> config.merge mutates args in place (folding a corpus's persisted
    # sources onto it), so a shared Namespace would leak corpus A's repos into
    # corpus B's merge. Simulate that mutation and assert B starts clean.
    monkeypatch.setattr(main_mod.cli_list, "all_corpora", lambda: ["aa", "bb"])
    monkeypatch.setattr(main_mod, "ensure_rfc_index", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "ensure_catalog_index", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "sync_if_pristine", lambda *a, **k: None)
    seen: List[tuple] = []

    def fake_gather_one(args: argparse.Namespace, _verbosity: Any, **_kw: Any) -> bool:
        seen.append((args.wg, list(args.github or [])))
        # Stand in for config.merge folding this corpus's persisted repos.
        args.github = list(args.github or []) + [f"{args.wg}/repo"]
        return True

    monkeypatch.setattr(main_mod, "_gather_one", fake_gather_one)
    monkeypatch.setattr(main_mod.sys, "argv", ["ietf-llm", "--all"])
    main_mod.main()
    # Under the bug, bb would arrive carrying ["aa/repo"].
    assert seen == [("aa", []), ("bb", [])]


# --- gather plan summary (shown at gather start) --------------------------


def _plan_args(**kw: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "months": 12,
        "new_drafts": False,
        "author": None,
        "draft": None,
        "mailing_list": None,
        "github": None,
        "add_mentioned_drafts": False,
        "include_related_drafts": False,
        "github_label": None,
        "exclude_github_label": None,
        "no_embed": False,
        "summarize": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_gather_plan_summary_lists_sources_and_scope() -> None:
    out = sequencer._gather_plan_summary(
        _plan_args(months=6, github=["httpwg/http-core"], mailing_list=["last-call"])
    )
    assert "months=6" in out
    assert "github: httpwg/http-core" in out
    assert "lists: last-call" in out
    assert "embed=on" in out


def test_gather_plan_summary_author_and_new_drafts() -> None:
    assert "author=mnot@mnot.net" in sequencer._gather_plan_summary(
        _plan_args(author="mnot@mnot.net")
    )
    assert "new-drafts" in sequencer._gather_plan_summary(_plan_args(new_drafts=True))


def test_gather_plan_summary_caps_long_lists() -> None:
    out = sequencer._gather_plan_summary(_plan_args(draft=["a", "b", "c", "d", "e"]))
    assert "a, b, c (+2 more)" in out


def test_gather_plan_summary_flags_no_embed() -> None:
    assert "embed=off" in sequencer._gather_plan_summary(_plan_args(no_embed=True))


def test_gather_plan_summary_annotates_config_source() -> None:
    # A surprising embed=off the user didn't ask for this run is traceable
    # to the global config; cli / default sources are left unannotated.
    out = sequencer._gather_plan_summary(
        _plan_args(no_embed=True, _global_sources={"no_embed": "config"})
    )
    assert "embed=off (from config)" in out

    plain = sequencer._gather_plan_summary(
        _plan_args(no_embed=False, _global_sources={"no_embed": "default"})
    )
    assert "embed=on" in plain and "(from" not in plain
