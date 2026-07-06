"""Stage planning and progress tracking for the gather pipeline.

Split out so the long pipeline orchestrator (`gather.sequencer._gather_one`)
and the MCP gather runner share one definition of *which* stages run, in
*what* order, for a given corpus shape. `stage_plan` is the single source
of truth; `StageTracker` drives the progress callback and raises if the
inline `begin(...)` sequence in `_gather_one` ever drifts from the plan.
"""

from __future__ import annotations

import argparse
from typing import Callable, List, Optional

#: A progress callback receives `(stage_name, index, total, detail)`: once as
#: each gather stage begins (1-based index, `detail=None`), and optionally again
#: mid-stage with a human-readable `detail` string for long stages — e.g. the
#: mailing-list download reporting "1240/8000 messages downloaded". The CLI
#: passes None; the MCP gather runner passes a callback that writes the
#: per-corpus status file.
ProgressFn = Callable[[str, int, int, Optional[str]], None]


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
        "message citations",
        "people",
        "timeline",
        "digests",
    ]
    if not args.no_embed:
        plan += ["embedding index", "topic map"]
    return plan


class StageTracker:
    """Drives the progress callback and guards against drift between the
    inline stage sequence and `stage_plan`."""

    def __init__(self, plan: List[str], progress: Optional[ProgressFn]) -> None:
        self._plan = plan
        self._progress = progress
        self._i = 0
        self._name: Optional[str] = None

    def begin(self, name: str) -> None:
        expected = self._plan[self._i] if self._i < len(self._plan) else None
        if name != expected:
            raise RuntimeError(
                f"gather stage drift: began {name!r} at position {self._i}, "
                f"but stage_plan expects {expected!r}"
            )
        self._i += 1
        self._name = name
        if self._progress is not None:
            self._progress(name, self._i, len(self._plan), None)

    def detail(self, text: str) -> None:
        """Report mid-stage progress for the stage currently in flight (e.g. a
        download counter on a long stage). A no-op before any stage has begun or
        when there is no progress sink. The reporter coalesces these — see the
        MCP gather runner's `_progress` — so a chatty stage can call it freely."""
        if self._progress is not None and self._name is not None:
            self._progress(self._name, self._i, len(self._plan), text)
