"""Redaction of the gather status fails closed, and applies at read time too: a
record stored before the writer redacted (or by an older host in a mixed fleet)
must not show a URL's credentials, and a redaction bug must never put the
unsanitised text on disk or in the fleet record. Also pins the child's stderr
redaction through the real `__main__` entry point."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ietf_llm import redact as redact_module
from ietf_llm.gather import lifecycle
from ietf_llm.gather.lifecycle import StatusRecorder
from ietf_llm.mcp import gather as mcp_gather
from ietf_llm.redact import PLACEHOLDER
from ietf_llm.store.corpus import LocalCorpusStore

SECRET_URL = "https://svc:s3cret-token@embed.example/v1/embeddings"
CLEAN_URL = "https://***@embed.example/v1/embeddings"


def _assert_clean(*records: Any) -> None:
    blob = json.dumps(records)
    for fragment in ("s3c", "svc:", "svc@"):
        assert fragment not in blob, fragment


class _Store(LocalCorpusStore):
    def __init__(self) -> None:
        self.statuses: List[Dict[str, Any]] = []

    def put_gather_status(self, corpus: str, status: Dict[str, Any]) -> None:
        self.statuses.append(json.loads(json.dumps(status)))


def _raise(_value: Any) -> Any:
    raise RuntimeError("boom " + SECRET_URL)


def test_write_status_never_writes_unsanitised_text_when_redaction_raises(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(redact_module, "redact_value", _raise)
    store = _Store()
    recorder = StatusRecorder(
        store, lifecycle.new_status("tls", {}, "running", driver="cli")
    )
    recorder.progress("drafts", 1, 2, f"from {SECRET_URL}")
    recorder.note(f"fetching {SECRET_URL}")
    recorder.finish("failed", f"ConnectionError: {SECRET_URL}")
    final = store.statuses[-1]
    assert final["state"] == "failed"
    assert final["error"] == final["stage_detail"] == PLACEHOLDER
    assert final["notes"] == [PLACEHOLDER]
    _assert_clean(
        store.statuses,
        Path(lifecycle.status_path("tls")).read_text(),
        capsys.readouterr().err,
    )


def test_failed_redaction_keeps_only_allowlisted_scalars(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `spec` is the caller's own input and an unknown key is anything a future
    # change adds: neither may survive a failed redaction.
    monkeypatch.setattr(redact_module, "redact_value", _raise)
    status = lifecycle.new_status(
        "tls",
        {"author": SECRET_URL, "mailing_list": [SECRET_URL]},
        "running",
        driver="cli",
    )
    status["extra"] = {"url": SECRET_URL}
    status["detail"] = SECRET_URL
    status["stage"] = "drafts"
    store = _Store()
    lifecycle.write_status(store, status)
    written = store.statuses[-1]
    assert written["spec"] == PLACEHOLDER
    assert "extra" not in written and "detail" not in written
    assert written["corpus"] == "tls" and written["state"] == "running"
    assert written["stage"] == "drafts" and written["pid"] == status["pid"]
    _assert_clean(
        store.statuses,
        Path(lifecycle.status_path("tls")).read_text(),
        capsys.readouterr().err,
    )


def test_write_status_survives_a_record_json_cannot_serialise(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = lifecycle.new_status("tls", {}, "running", driver="cli")
    status["oops"] = object()
    lifecycle.write_status(_Store(), status)  # must not raise
    assert "could not write status for tls" in capsys.readouterr().err

    # A cyclic record cannot be redacted, so the fallback replaces it with the
    # allowlisted fields and the write succeeds.
    cyclic: Dict[str, Any] = {}
    cyclic["self"] = cyclic
    status["oops"] = cyclic
    store = _Store()
    lifecycle.write_status(store, status)
    assert "could not sanitise" in capsys.readouterr().err
    assert store.statuses[-1]["state"] == "running"
    assert "oops" not in store.statuses[-1]


def test_a_failed_redaction_is_warned_about(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(redact_module, "redact_value", _raise)
    status = lifecycle.new_status("tls", {}, "failed", driver="cli")
    status["error"] = SECRET_URL
    lifecycle.write_status(_Store(), status)
    assert (
        "could not sanitise a status record (RuntimeError)" in capsys.readouterr().err
    )


def test_gather_status_output_redacts_records_stored_unredacted() -> None:
    record = {
        "corpus": "tls",
        "state": "failed",
        "error": f"ConnectionError: POST {SECRET_URL} failed",
        "notes": [f"seed {SECRET_URL}"],
    }
    out = mcp_gather._format_gather_status(record)
    assert CLEAN_URL in out
    _assert_clean(out)
    assert SECRET_URL in json.dumps(record)  # the stored record is not rewritten


def test_gather_status_output_redacts_running_detail() -> None:
    out = mcp_gather._format_gather_status(
        {"corpus": "tls", "state": "running", "stage_detail": f"from {SECRET_URL}"}
    )
    assert CLEAN_URL in out
    _assert_clean(out)


def test_gather_status_output_survives_redaction_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redact_module, "redact_value", _raise)
    out = mcp_gather._format_gather_status(
        {"corpus": "tls", "state": "failed", "error": SECRET_URL, "notes": [SECRET_URL]}
    )
    assert PLACEHOLDER in out
    _assert_clean(out)


def test_child_run_as_main_prints_a_redacted_traceback() -> None:
    # Pins the `sys.excepthook` wiring under `__main__`, which calling the hook
    # function directly does not.
    script = (
        "import runpy, sys\n"
        "import ietf_llm.gather.sequencer as seq\n"
        "def boom(*args, **kwargs):\n"
        f"    raise ConnectionError('POST {SECRET_URL} failed')\n"
        "seq.run_gather = boom\n"
        "sys.argv = ['child', 'tls']\n"
        "runpy.run_module('ietf_llm.gather.child', run_name='__main__')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 1
    assert f"ConnectionError: POST {CLEAN_URL} failed" in proc.stderr
    _assert_clean(proc.stderr)
