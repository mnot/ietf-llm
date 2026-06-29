"""`_sync_one_list` retry + outcome reporting. The single IMAP attempt is
stubbed; what's under test is the orchestration around it — retry-once on a
transient fault, no retry on a folder-select failure, and an explicit
STATUS/WARN line for every outcome so a silent empty sync can't pass as
success."""

from __future__ import annotations

import imaplib

import pytest

from ietf_llm.gather import mbox
from ietf_llm.utils import Verbosity


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(mbox, "get_cache_dir", lambda: str(tmp_path))


def _run(monkeypatch, attempt):
    monkeypatch.setattr(mbox, "_imap_sync_attempt", attempt)
    return mbox._sync_one_list("netconf", "netconf", 12, Verbosity.STATUS)


def test_success_reports_count(monkeypatch, capsys):
    uids = _run(monkeypatch, lambda *a: (["1", "2", "3"], 1))
    assert uids == ["1", "2", "3"]
    err = capsys.readouterr().err
    assert "Synced 'netconf': 3 message(s) in the last 12 month(s) (1 new)." in err


def test_empty_window_warns(monkeypatch, capsys):
    uids = _run(monkeypatch, lambda *a: ([], 0))
    assert uids == []
    err = capsys.readouterr().err
    assert "[WARN]" in err
    assert "No messages for 'netconf' in the last 12 month(s)" in err


def test_folder_select_error_not_retried(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        raise mbox._FolderSelectError("netconf")

    uids = _run(monkeypatch, attempt)
    assert uids == []
    assert calls["n"] == 1  # not retried
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "Could not select the IMAP folder for 'netconf'" in err


def test_transient_error_retried_then_succeeds(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise imaplib.IMAP4.error("connection reset")
        return (["7"], 1)

    uids = _run(monkeypatch, attempt)
    assert uids == ["7"]
    assert calls["n"] == 2  # one retry
    err = capsys.readouterr().err
    assert "[WARN]" in err and "retrying" in err
    assert "Synced 'netconf': 1 message(s)" in err


def test_transient_error_exhausts_retries(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        raise OSError("timed out")

    uids = _run(monkeypatch, attempt)
    assert uids == []
    assert calls["n"] == mbox.IMAP_RETRIES + 1
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "usually transient" in err
