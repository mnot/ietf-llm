"""Real-subprocess coverage for the gather runner's out-of-process pipeline.

The rest of the suite runs the pipeline in-process (the `_gather_in_process`
autouse fixture) so it can stub `run_gather`. These tests exercise the actual
subprocess machinery — the child's progress protocol, the parent's streaming
pump, the exit-code mapping, and cancellation — without touching the network:
the child protocol is driven with a stubbed `run_gather`, and the parent pump is
driven against a tiny fake child script.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from ietf_llm import gather_child, gather_pipeline, gather_runner
from ietf_llm.gather_runner import GatherSpec


# --- the child side: emit protocol + exit codes ----------------------------


def _run_child_capturing(monkeypatch: pytest.MonkeyPatch, ok: bool) -> tuple:
    """Run `gather_child.main()` with a stubbed pipeline and capture the JSON
    records it writes to the progress fd plus its exit code."""
    read_fd, write_fd = os.pipe()

    def fake_run_gather(argv, _verbosity, progress=None, note_fn=None):  # type: ignore[no-untyped-def]
        progress("charter", 1, 3, None)
        progress("mailing list", 2, 3, "5/5 messages")
        if note_fn:
            note_fn("auto-tracked o/r")
        return ok

    monkeypatch.setattr(gather_child, "run_gather", fake_run_gather)
    monkeypatch.setenv("IETF_LLM_PROGRESS_FD", str(write_fd))

    code = {}

    def _go() -> None:
        # main() owns write_fd (it fdopens and closes it), so the read end sees
        # EOF without the test closing anything.
        try:
            gather_child.main()
        except SystemExit as exc:
            code["rc"] = exc.code

    thread = threading.Thread(target=_go)
    thread.start()
    with os.fdopen(read_fd, "r", encoding="utf-8") as pipe:
        lines = [json.loads(line) for line in pipe if line.strip()]
    thread.join()
    return lines, code.get("rc")


def test_child_emits_progress_notes_and_success_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines, rc = _run_child_capturing(monkeypatch, ok=True)
    kinds = [r["t"] for r in lines]
    assert kinds == ["progress", "progress", "note"]
    assert lines[0] == {
        "t": "progress", "name": "charter", "index": 1, "total": 3, "detail": None
    }
    assert lines[1]["detail"] == "5/5 messages"
    assert lines[2] == {"t": "note", "msg": "auto-tracked o/r"}
    assert rc == 0


def test_child_unusable_corpus_maps_to_distinct_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lines, rc = _run_child_capturing(monkeypatch, ok=False)
    assert rc == gather_pipeline.CHILD_EXIT_UNUSABLE


# --- the parent side: stream a fake child, map exit codes ------------------


def _fake_child(tmp_path: Path) -> Path:
    """A stand-in for `ietf_llm.gather_child`: emits two records, then exits with
    `FAKE_EXIT` (optionally sleeping first, for the cancellation test)."""
    script = tmp_path / "fake_child.py"
    script.write_text(
        "import os, sys, json, time\n"
        "w = os.fdopen(int(os.environ['IETF_LLM_PROGRESS_FD']), 'w')\n"
        "w.write(json.dumps({'t':'progress','name':'charter',"
        "'index':1,'total':2,'detail':None})+chr(10)); w.flush()\n"
        "w.write(json.dumps({'t':'note','msg':'hi'})+chr(10)); w.flush()\n"
        "if os.environ.get('FAKE_SLEEP'): time.sleep(30)\n"
        "w.close()\n"
        "sys.exit(int(os.environ.get('FAKE_EXIT','0')))\n"
    )
    return script


def _collect(spec: GatherSpec, cancel: bool = False) -> tuple:
    prog: list = []
    notes: list = []
    ok = gather_pipeline._run_pipeline_child(
        spec,
        lambda name, index, total, detail=None: prog.append(
            (name, index, total, detail)
        ),
        notes.append,
        lambda: cancel,
    )
    return ok, prog, notes


def test_parent_streams_records_and_maps_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _fake_child(tmp_path)
    monkeypatch.setattr(
        gather_pipeline, "_child_command", lambda spec: [sys.executable, str(script)]
    )
    monkeypatch.setenv("FAKE_EXIT", "0")
    ok, prog, notes = _collect(GatherSpec(corpus="wg"))
    assert ok is True
    assert prog == [("charter", 1, 2, None)]
    assert notes == ["hi"]
    assert "wg" not in gather_pipeline._children  # unregistered on the way out


def test_parent_maps_unusable_and_crash_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _fake_child(tmp_path)
    monkeypatch.setattr(
        gather_pipeline, "_child_command", lambda spec: [sys.executable, str(script)]
    )
    # Exit 3 -> unusable corpus (run_gather False), not a crash.
    monkeypatch.setenv("FAKE_EXIT", str(gather_pipeline.CHILD_EXIT_UNUSABLE))
    ok, _prog, _notes = _collect(GatherSpec(corpus="wg"))
    assert ok is False
    # Any other non-zero exit is an unexpected death -> RuntimeError.
    monkeypatch.setenv("FAKE_EXIT", "1")
    with pytest.raises(RuntimeError):
        _collect(GatherSpec(corpus="wg"))


def test_parent_cancellation_terminates_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _fake_child(tmp_path)
    monkeypatch.setattr(
        gather_pipeline, "_child_command", lambda spec: [sys.executable, str(script)]
    )
    monkeypatch.setenv("FAKE_EXIT", "0")
    monkeypatch.setenv("FAKE_SLEEP", "1")  # the child would otherwise run 30s
    # The cancel-check returns True (as `lambda: _cancel_requested(corpus)` would
    # once a stop is requested), so the pump must kill the child and unwind.
    started = time.monotonic()
    with pytest.raises(gather_runner.GatherCancelled):
        _collect(GatherSpec(corpus="wg"), cancel=True)
    # Returns promptly (well under the child's 30s sleep) and leaves no
    # registered child behind.
    assert time.monotonic() - started < 20
    assert "wg" not in gather_pipeline._children
