"""The boundary between the server and the regex-scan child process.

The child is untrusted-ish in the sense that matters here: it can be stuck,
crash, or answer with garbage, and none of that may read as "no matches" or
leak its stderr (server paths, tracebacks) to a client of the shared server.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from ietf_llm import regexscan, serve_metrics
from ietf_llm.embeddings.storage import _open_db
from ietf_llm.mcp import grep, grep_rfc, rfc_text
from ietf_llm.mcp.grep import tool_grep_corpus

from conftest import write_cache_file

_EVIL = r"(\w|\w)*X"
_EVIL_TEXT = "a" * 40 + "!\n"

posix_only = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="needs SIGALRM and /bin/sh"
)


@posix_only
def test_the_alarm_backstop_ends_a_child_nobody_is_waiting_for(tmp_path: Path) -> None:
    """If the server dies mid-scan nothing is left to kill the child, so it
    must end itself. Drive the child by hand and never collect its answer: the
    kernel has to deliver SIGALRM to a process stuck inside a C regex call."""
    path = tmp_path / "evil.md"
    path.write_text(_EVIL_TEXT, encoding="utf-8")
    request = {
        "kind": "files",
        "pattern": _EVIL,
        "flags": 0,
        "files": [["evil.md", str(path)]],
        "keep": 1,
        "per_file_cap": None,
        "budget": 0.2,
        "alarm_s": 1,
    }
    with subprocess.Popen(
        [sys.executable, "-m", "ietf_llm.regexscan"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    ) as proc:
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request).encode())
            proc.stdin.close()
            assert proc.wait(timeout=10) == -signal.SIGALRM
        finally:
            proc.kill()


def _stub_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Have `run_bounded` start a /bin/sh body in place of the real child."""
    stub = tmp_path / "stub-python"
    stub.write_text(f"#!/bin/sh\ncat >/dev/null\n{script}\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(regexscan.sys, "executable", str(stub))


@posix_only
@pytest.mark.parametrize(
    "script",
    ["printf 'not-json'", "true", "printf '[1]'", 'printf \'{"complete": true}\''],
)
def test_malformed_child_output_is_scan_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    """Exit 0 with empty, non-JSON, non-object, or key-less output must be a
    ScanFailed, not a JSONDecodeError / KeyError surfacing as a tool error."""
    _stub_child(tmp_path, monkeypatch, script)
    with pytest.raises(regexscan.ScanFailed):
        regexscan.run_bounded({"kind": "files"}, 5.0, ("hits",))


@posix_only
def test_child_stderr_stays_in_the_server_log(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve_metrics.reset()
    write_cache_file(isolated_home, "wg", "threads/a.md", "needle\n")
    _stub_child(
        tmp_path,
        monkeypatch,
        "echo 'Traceback: File /srv/app/secret/path.py' >&2; exit 3",
    )
    out = tool_grep_corpus("wg", "need.e", regex=True)
    assert "Nothing can be concluded" in out
    assert "secret" not in out and "Traceback" not in out
    err = capsys.readouterr().err
    assert "/srv/app/secret/path.py" in err
    assert 'ietf_llm_grep_scan_stops_total{reason="failed"} 1' in serve_metrics.render()


def test_a_kill_is_logged_and_counted(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(grep, "_REGEX_SCAN_BUDGET_S", 0.5)
    monkeypatch.setattr(regexscan, "KILL_SLACK_S", 0.5)
    serve_metrics.reset()
    write_cache_file(isolated_home, "wg", "threads/evil.md", _EVIL_TEXT)
    out = tool_grep_corpus("wg", _EVIL, regex=True)
    assert "INCOMPLETE SCAN" in out
    assert "regex scan timeout" in capsys.readouterr().err
    assert (
        'ietf_llm_grep_scan_stops_total{reason="timeout"} 1' in serve_metrics.render()
    )


def test_rfc_regex_soft_stop_between_sections(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regex that is slow but returns stops at the budget between sections,
    in the child, and reports how far it got — as the literal path does."""
    assert isolated_home
    conn = _open_db(rfc_text.RFC_CORPUS)
    try:
        vec = np.ones(4, dtype=np.float32).tobytes()
        conn.executemany(
            "INSERT INTO chunks(file, chunk_idx, sub_idx, title, text, embedding, "
            "section) VALUES(?,0,0,'Intro',?,?,'1')",
            [("rfc1.txt", "caching one", vec), ("rfc2.txt", "caching two", vec)],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(grep_rfc, "_RFC_SCAN_BUDGET_S", -1.0)
    monkeypatch.setattr(grep_rfc, "_RFC_SCAN_CHECK_EVERY", 1)
    out = tool_grep_corpus("rfcs", "cach.ng", regex=True)
    assert "INCOMPLETE SCAN" in out
    assert "1 of the corpus's section(s)" in out
    assert "evidence of absence" not in out
