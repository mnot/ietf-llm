"""Tests for the gather stage plan and progress tracker
(ietf_llm.gather.stages), plus the writer-side guard that the inline
`tracker.begin(...)` sequence in `_gather_one` matches `stage_plan`.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pytest

from ietf_llm.gather.stages import StageTracker, stage_plan


def _args(**kw: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "github": None,
        "draft": None,
        "author": None,
        "new_drafts": False,
        "no_embed": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


# --- stage_plan -----------------------------------------------------------


def test_stage_plan_group_includes_datatracker_stages() -> None:
    plan = stage_plan(_args(), group_backed=True)
    for stage in ("charter", "meetings", "transcripts", "documents"):
        assert stage in plan
    # Always-on stages are present too, in order.
    assert plan[0] == "charter"
    # The topic map is the final stage — it clusters the just-built index.
    assert plan[-2:] == ["embedding index", "topic map"]
    assert plan.index("mailing list") < plan.index("identity registry")


def test_stage_plan_custom_omits_group_stages() -> None:
    plan = stage_plan(_args(), group_backed=False)
    for stage in ("charter", "meetings", "transcripts", "documents"):
        assert stage not in plan
    assert "mailing list" in plan
    assert "embedding index" in plan


def test_stage_plan_github_and_drafts_gated() -> None:
    without = stage_plan(_args(), group_backed=False)
    assert "github archives" not in without
    assert "github issues" not in without
    assert "drafts" not in without

    withx = stage_plan(
        _args(github=["o/r"], draft=["draft-x"]), group_backed=False
    )
    assert "github archives" in withx
    assert "github issues" in withx
    assert "drafts" in withx


def test_stage_plan_no_embed_drops_index_stage() -> None:
    assert "embedding index" not in stage_plan(
        _args(no_embed=True), group_backed=False
    )


def test_stage_plan_dynamic_drafts_count_as_drafts_stage() -> None:
    assert "drafts" in stage_plan(_args(author="mnot@mnot.net"), group_backed=False)
    assert "drafts" in stage_plan(_args(new_drafts=True), group_backed=False)


# --- StageTracker ---------------------------------------------------------


def test_stage_tracker_emits_index_and_total_in_order() -> None:
    plan = ["a", "b", "c"]
    seen: List[Any] = []
    tracker = StageTracker(plan, lambda n, i, t, d: seen.append((n, i, t, d)))
    tracker.begin("a")
    tracker.begin("b")
    tracker.begin("c")
    # Each stage begins with detail=None.
    assert seen == [("a", 1, 3, None), ("b", 2, 3, None), ("c", 3, 3, None)]


def test_stage_tracker_detail_reports_current_stage() -> None:
    seen: List[Any] = []
    tracker = StageTracker(["a", "b"], lambda n, i, t, d: seen.append((n, i, t, d)))
    tracker.begin("a")
    tracker.detail("1/10 done")  # mid-stage update for the in-flight stage
    tracker.begin("b")
    assert seen == [
        ("a", 1, 2, None),
        ("a", 1, 2, "1/10 done"),  # same stage/index, carries detail
        ("b", 2, 2, None),
    ]


def test_stage_tracker_detail_before_any_stage_is_noop() -> None:
    seen: List[Any] = []
    tracker = StageTracker(["a"], lambda n, i, t, d: seen.append((n, i, t, d)))
    tracker.detail("nope")  # no stage begun yet
    assert seen == []


def test_stage_tracker_none_progress_is_noop() -> None:
    tracker = StageTracker(["a"], None)
    tracker.begin("a")  # must not raise
    tracker.detail("x")  # must not raise


def test_stage_tracker_raises_on_drift() -> None:
    tracker = StageTracker(["a", "b"], None)
    with pytest.raises(RuntimeError, match="stage drift"):
        tracker.begin("b")  # expected "a" first


def test_stage_plan_author_adds_reviews_and_mail() -> None:
    """Both author passes are their own stages: each is minutes of
    network work that would otherwise look like a hung 'drafts'."""
    plan = stage_plan(_args(author="mnot@mnot.net"), group_backed=False)
    assert plan.index("drafts") < plan.index("reviews") < plan.index("author mail")
    # Author mail writes .eml the registry consolidates, so it must land
    # before the registry is built.
    assert plan.index("author mail") < plan.index("identity registry")


def test_stage_plan_without_author_has_neither() -> None:
    plan = stage_plan(_args(), group_backed=False)
    assert "reviews" not in plan
    assert "author mail" not in plan
