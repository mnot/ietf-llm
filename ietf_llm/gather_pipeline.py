"""Runs one gather's pipeline, out of the server process.

`gather_runner` owns the queue, leases, heartbeat, and status record; this
module is the seam where the actual pipeline runs. In production it spawns a
child process (`ietf_llm.gather_child`) and streams the child's stage progress
back into the caller's `progress`/`note` callbacks, so a CPU-heavy stage — the
embedding-index build — can never hold the GIL and stall the server's event
loop (a read that stalled during that stage used to be indistinguishable from a
dead server). The worker thread blocks on the child's pipe, which releases the
GIL, so the server stays responsive throughout.

Under `IETF_LLM_GATHER_INPROCESS` (the test suite, which stubs `run_gather` by
monkeypatch — invisible across a process boundary) it calls `run_gather`
directly in this process instead.

This module deliberately does not import `gather_runner`: the cancel check comes
in as a callback and cancellation raises `GatherCancelled` (defined here,
re-exported by `gather_runner` for callers), so the dependency runs one way
(`gather_runner` -> `gather_pipeline`) with no cycle.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .utils import Verbosity

if TYPE_CHECKING:
    from .gather_runner import GatherSpec

#: How often the parent wakes to poll for a cancel while a gather subprocess
#: runs silently (e.g. deep in the embedding stage, which emits no progress
#: records). Bounds cancellation latency; each poll is one cancel check.
_CHILD_POLL_S = 4.0

#: Grace a terminated gather subprocess gets to exit before it is killed.
_CHILD_TERM_GRACE_S = 5.0

#: Exit code the gather subprocess uses for "the corpus name was unusable"
#: (`run_gather` returned False), as opposed to a crash — so a bad name still
#: reads as the actionable "not a recognized corpus" message. The child imports
#: this so the two sides of the protocol can't drift.
CHILD_EXIT_UNUSABLE = 3

#: A stage-progress callback (name, index, total, detail), a one-line note
#: callback, and a "has a stop been requested?" predicate — matching the
#: closures `gather_runner._run_one` builds. `run_pipeline` feeds these whether
#: it runs the pipeline in this process or streams a subprocess's records back.
ProgressCallback = Callable[[str, int, int, Optional[str]], None]
NoteCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]

#: Live gather subprocesses, corpus -> Popen, with their own lock (this module
#: owns them). The worker threads are daemons, so without `_reap_children`
#: (registered with atexit) the parent's exit would orphan a running child.
_children_lock = threading.Lock()
_children: "Dict[str, subprocess.Popen[bytes]]" = {}


class GatherCancelled(BaseException):
    """Raised when a stop is honoured, to unwind the gather to a terminal
    `cancelled` status. Subclasses `BaseException` so a stage's broad
    `except Exception` cannot swallow the stop signal."""


def _gather_in_process() -> bool:
    """Whether to run the pipeline in this process instead of a subprocess.

    Off in production (a child isolates a CPU-heavy stage from the server); on
    under `IETF_LLM_GATHER_INPROCESS` for the test suite, which stubs the
    pipeline by monkeypatch — invisible across a process boundary."""
    return os.environ.get("IETF_LLM_GATHER_INPROCESS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def run_pipeline(
    spec: "GatherSpec",
    progress: ProgressCallback,
    note: NoteCallback,
    cancel_requested: CancelCheck,
) -> bool:
    """Run the gather pipeline for `spec`, feeding `progress`/`note` exactly as
    the in-process path always did. In production this drives a child process
    and streams its progress back; under the in-process test flag it calls
    `run_gather` directly.

    Returns True on success, False if the corpus name was unusable. Raises
    `GatherCancelled` if a stop is honoured, and `RuntimeError` if the
    subprocess dies unexpectedly."""
    if _gather_in_process():
        from . import __main__ as gather_main  # pylint: disable=import-outside-toplevel

        return gather_main.run_gather(
            spec.to_argv(), Verbosity.STATUS, progress=progress, note_fn=note
        )
    return _run_pipeline_child(spec, progress, note, cancel_requested)


def _child_command(spec: "GatherSpec") -> List[str]:
    """The argv that runs one gather as a child process — the same corpus +
    source argv `to_argv` builds for the in-process path, under
    `ietf_llm.gather_child`. A seam so a test can substitute a fake child
    without spawning the real pipeline (and its network)."""
    return [sys.executable, "-m", "ietf_llm.gather_child", *spec.to_argv()]


def _run_pipeline_child(
    spec: "GatherSpec",
    progress: ProgressCallback,
    note: NoteCallback,
    cancel_requested: CancelCheck,
) -> bool:
    """Spawn the gather as a child process and stream its progress records back
    into `progress`/`note` until it exits. The worker thread blocks on the
    child's pipe (GIL released), so the server stays responsive throughout."""
    corpus = spec.corpus
    read_fd, write_fd = os.pipe()
    env = dict(os.environ)
    env["IETF_LLM_PROGRESS_FD"] = str(write_fd)
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            _child_command(spec),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            env=env,
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(write_fd)  # the child holds the writer now; the parent only reads
    with _children_lock:
        _children[corpus] = proc
    try:
        # Owns + closes read_fd.
        _pump_child(corpus, proc, read_fd, progress, note, cancel_requested)
        rc = proc.wait()
    finally:
        _terminate_child(proc)  # no-op if it already exited; kills on the cancel path
        with _children_lock:
            if _children.get(corpus) is proc:
                del _children[corpus]
    if rc == 0:
        return True
    if rc == CHILD_EXIT_UNUSABLE:
        return False  # bad corpus name — same as run_gather returning False
    raise RuntimeError(f"gather subprocess for {corpus!r} exited with code {rc}")


def _pump_child(
    corpus: str,
    proc: "subprocess.Popen[bytes]",
    read_fd: int,
    progress: ProgressCallback,
    note: NoteCallback,
    cancel_requested: CancelCheck,
) -> None:
    """Feed the child's progress records into `progress`/`note` until it closes
    the pipe, checking for a cancel between records and on an idle tick.

    A daemon reader thread does the blocking line reads (GIL released) and hands
    records to this thread over a queue; this thread applies them and owns the
    cancel decision. On a cancel it terminates the child, which EOFs the pipe
    and ends the reader. Portable (no `select`), so it works off POSIX too."""
    records: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()

    def _reader() -> None:
        try:
            with os.fdopen(read_fd, "r", encoding="utf-8") as pipe:
                # readline (not `for line in pipe`) so a line is delivered as
                # soon as it arrives, not after a read-ahead buffer fills.
                for line in iter(pipe.readline, ""):
                    record = _parse_record(line)
                    if record is not None:
                        records.put(record)
        except (OSError, ValueError):
            pass
        finally:
            records.put(None)  # sentinel: the pipe closed / the child is gone

    reader = threading.Thread(target=_reader, name=f"gather-pipe-{corpus}", daemon=True)
    reader.start()
    while True:
        try:
            record = records.get(timeout=_CHILD_POLL_S)
        except queue.Empty:
            if cancel_requested():
                _terminate_child(proc)
                raise GatherCancelled(corpus) from None
            continue
        if record is None:
            return  # reader hit EOF; the child is finishing
        if cancel_requested():
            _terminate_child(proc)
            raise GatherCancelled(corpus)
        _apply_record(record, progress, note)


def _parse_record(line: str) -> Optional[Dict[str, Any]]:
    """A progress/note record from one JSON line, or None if unparseable."""
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def _apply_record(
    record: Dict[str, Any], progress: ProgressCallback, note: NoteCallback
) -> None:
    kind = record.get("t")
    if kind == "progress":
        progress(
            str(record.get("name")),
            int(record.get("index") or 0),
            int(record.get("total") or 0),
            record.get("detail"),
        )
    elif kind == "note":
        message = record.get("msg")
        if message:
            note(str(message))


def _terminate_child(proc: "subprocess.Popen[bytes]") -> None:
    """Best-effort stop a gather subprocess: nothing to do if it already exited,
    otherwise SIGTERM, then SIGKILL if it doesn't go within the grace window."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=_CHILD_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_CHILD_TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            pass


def _reap_children() -> None:
    """Terminate any live gather subprocess on interpreter exit — the worker
    threads are daemons, so nothing else would."""
    with _children_lock:
        procs = list(_children.values())
    for proc in procs:
        _terminate_child(proc)


atexit.register(_reap_children)
