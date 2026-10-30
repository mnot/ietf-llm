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

Invoked as `python -m ietf_llm.gather.child <corpus> [--source ...]` — the same
argv `GatherSpec.to_argv()` produces for the in-process path.

Exit codes: the `GatherOutcome` run_gather returned, as `pipeline.OUTCOME_EXIT_CODES`
maps it (0 = gathered, 3 = the corpus name was unusable, 4 = skipped, nothing to
publish); anything else = an unhandled error (traceback on the inherited
stderr, and an `error` record on the pipe so the parent's status can say what
failed) or a signal (the parent terminates the child to cancel).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from ..embeddings.oai_compat import redact_userinfo
from ..log import Verbosity
from .pipeline import OUTCOME_EXIT_CODES
from .sequencer import run_gather

#: Longest exception message sent to the parent. The parent's status record
#: truncates again; this keeps one runaway message from filling the pipe.
_ERROR_MSG_MAX = 300


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
        outcome = run_gather(
            sys.argv[1:], Verbosity.STATUS, progress=_progress, note_fn=_note
        )
    except Exception as err:
        # stderr is inherited and lost to a fleet operator; the parent's status
        # record is what `gather_status` shows, so say what failed there, minus
        # any credentials embedded in a URL the exception quotes.
        message = redact_userinfo(" ".join(str(err).split()))
        _emit(
            {
                "t": "error",
                "type": type(err).__name__,
                "msg": message[:_ERROR_MSG_MAX],
            }
        )
        raise
    finally:
        # Close the writer so the parent's reader sees EOF and stops pumping,
        # even if run_gather raises (the traceback still reaches stderr).
        if sink is not None:
            sink.close()
    sys.exit(OUTCOME_EXIT_CODES[outcome])


if __name__ == "__main__":
    main()
