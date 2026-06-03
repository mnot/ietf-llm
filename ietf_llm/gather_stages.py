"""Stage planning and progress tracking for the gather pipeline.

Split out of `__main__` so the long pipeline orchestrator (`_gather_one`)
and the MCP gather runner share one definition of *which* stages run, in
*what* order, for a given corpus shape. `stage_plan` is the single source
of truth; `StageTracker` drives the progress callback and raises if the
inline `begin(...)` sequence in `_gather_one` ever drifts from the plan.
"""

from __future__ import annotations

import argparse
from typing import Callable, List, Optional

#: A progress callback receives `(stage_name, index, total)` once as each
#: gather stage begins (1-based index). The CLI passes None; the MCP gather
#: runner passes a callback that writes the per-corpus status file.
ProgressFn = Callable[[str, int, int], None]


def stage_plan(args: argparse.Namespace, group_backed: bool) -> List[str]:
    """The ordered names of the stages `_gather_one` will run for this
    corpus, given its shape and configured sources.

    MUST stay in lockstep with the `tracker.begin(...)` calls in
    `_gather_one` (a `StageTracker` raises if they drift, and a test
    asserts the emitted sequence equals this plan). Stages whose work is
    gated by shape or flags are listed only when they will actually run.
    """
    has_github = bool(args.github)
    do_drafts = bool(args.draft or args.author or args.new_drafts)
    plan: List[str] = []
    if group_backed:
        plan += ["charter", "meetings"]
    plan.append("mailing list")
    if group_backed:
        plan += ["transcripts", "documents"]
    if do_drafts:
        plan.append("drafts")
    plan.append("pdf text")
    if has_github:
        plan.append("github archives")
    plan.append("identity registry")
    if has_github:
        plan.append("github issues")
    plan += [
        "issue files",
        "thread files",
        "citations",
        "people",
        "timeline",
        "digests",
    ]
    if not args.no_embed:
        plan.append("embedding index")
    return plan


class StageTracker:
    """Drives the progress callback and guards against drift between the
    inline stage sequence and `stage_plan`."""

    def __init__(self, plan: List[str], progress: Optional[ProgressFn]) -> None:
        self._plan = plan
        self._progress = progress
        self._i = 0

    def begin(self, name: str) -> None:
        expected = self._plan[self._i] if self._i < len(self._plan) else None
        if name != expected:
            raise RuntimeError(
                f"gather stage drift: began {name!r} at position {self._i}, "
                f"but stage_plan expects {expected!r}"
            )
        self._i += 1
        if self._progress is not None:
            self._progress(name, self._i, len(self._plan))
