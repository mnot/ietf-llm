"""Subprocess entry point for a single gather.

The MCP server runs each gather in a child process (this module) rather than an
in-process thread, so a CPU-heavy stage — notably the embedding-index build —
can never hold the GIL and stall the server's event loop (a read that stalled
during that stage was indistinguishable from a dead server). The child runs the
exact same pipeline as the in-process path (`run_gather`) and streams stage
progress back to the parent as newline-delimited JSON on the fd named by the
`IETF_LLM_PROGRESS_FD` env var; the parent turns those records back into the
status-record updates it already writes, so nothing downstream of the worker
changes.

Invoked as `python -m ietf_llm.gather_child <corpus> [--source ...]` — the same
argv `GatherSpec.to_argv()` produces for the in-process path.

Exit codes: 0 = the gather succeeded; 3 = the corpus name was unusable
(`run_gather` returned False); anything else = an unhandled error (traceback on
the inherited stderr) or a signal (the parent terminates the child to cancel).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from .__main__ import run_gather
from .gather_pipeline import CHILD_EXIT_UNUSABLE
from .utils import Verbosity


def _renice() -> None:
    """Drop this child's scheduling priority so a CPU-heavy stage (the embedding
    build) yields to the parent MCP server and keeps it responsive — a
    subprocess stops the child holding the GIL, but not competing for CPU.

    Best-effort and POSIX-only: unprivileged `os.nice` can only *lower* priority,
    which is exactly what we want. `IETF_LLM_GATHER_NICE` tunes the increment
    (default 10); 0 or negative disables it."""
    if not hasattr(os, "nice"):
        return
    try:
        increment = int(os.environ.get("IETF_LLM_GATHER_NICE", "10"))
    except ValueError:
        increment = 10
    if increment <= 0:
        return
    try:
        os.nice(increment)
    except OSError:
        pass


def main() -> None:
    _renice()
    fd_env = os.environ.get("IETF_LLM_PROGRESS_FD")
    sink = os.fdopen(int(fd_env), "w", encoding="utf-8") if fd_env else None

    def _emit(record: "dict[str, Any]") -> None:
        if sink is None:
            return
        try:
            sink.write(json.dumps(record) + "\n")
            sink.flush()
        except (OSError, ValueError):
            # The parent went away (crash / kill). The gather can still finish;
            # it just runs unobserved, and the parent reaps it on exit.
            pass

    def _progress(
        name: str, index: int, total: int, detail: Optional[str] = None
    ) -> None:
        _emit(
            {
                "t": "progress",
                "name": name,
                "index": index,
                "total": total,
                "detail": detail,
            }
        )

    def _note(message: str) -> None:
        _emit({"t": "note", "msg": message})

    try:
        ok = run_gather(
            sys.argv[1:], Verbosity.STATUS, progress=_progress, note_fn=_note
        )
    finally:
        # Close the writer so the parent's reader sees EOF and stops pumping,
        # even if run_gather raises (the traceback still reaches stderr).
        if sink is not None:
            sink.close()
    sys.exit(0 if ok else CHILD_EXIT_UNUSABLE)


if __name__ == "__main__":
    main()
