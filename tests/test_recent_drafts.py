"""Tests for the new-drafts subscription's rolling-window prune.

(`fetch_new_draft_names` hits the live submission API and is exercised
manually; here we cover the offline prune logic.)
"""

from __future__ import annotations

import os
from pathlib import Path

from ietf_llm.gather.sources.recent_drafts import prune_drafts
from ietf_llm.paths import drafts_dir


def _seed(cache: str, *names: str) -> None:
    d = drafts_dir(cache)
    os.makedirs(d, exist_ok=True)
    for name in names:
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("draft body\n")


def test_prune_removes_drafts_outside_the_window(tmp_path: Path) -> None:
    cache = str(tmp_path)
    _seed(
        cache,
        "draft-foo-bar-00.txt",
        "draft-foo-bar-01.txt",   # two revs of a kept draft
        "draft-stale-thing-00.txt",
        "rfc9999.txt",            # RFCs are never pruned
    )
    removed = prune_drafts(cache, ["draft-foo-bar"])
    assert removed == 1
    remaining = set(os.listdir(drafts_dir(cache)))
    assert remaining == {
        "draft-foo-bar-00.txt",
        "draft-foo-bar-01.txt",
        "rfc9999.txt",
    }


def test_prune_keeps_explicit_drafts(tmp_path: Path) -> None:
    cache = str(tmp_path)
    _seed(cache, "draft-window-thing-00.txt", "draft-pinned-00.txt")
    # keep includes the explicit --draft addition, so it survives even
    # though it's not in the (empty) window set.
    removed = prune_drafts(cache, ["draft-pinned"])
    assert removed == 1
    assert os.path.exists(os.path.join(drafts_dir(cache), "draft-pinned-00.txt"))


def test_prune_no_drafts_dir_is_noop(tmp_path: Path) -> None:
    assert prune_drafts(str(tmp_path / "nope"), ["draft-x"]) == 0
