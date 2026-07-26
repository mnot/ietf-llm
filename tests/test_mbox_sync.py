"""`_sync_one_list` retry + outcome reporting. The single IMAP attempt is
stubbed; what's under test is the orchestration around it — retry-once on a
transient fault, no retry on a folder-select failure, and one outcome line per
list delivered to BOTH the log (the CLI's channel) and `note_fn` (the MCP
client's, via gather_status) so a silent empty sync can't pass as success on
either."""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta

import pytest

from ietf_llm.gather.sources import mbox
from ietf_llm.log import Verbosity


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(mbox, "get_cache_dir", lambda: str(tmp_path))


def _run(monkeypatch, attempt):
    """Run one sync, returning (uids, stderr-text, notes)."""
    monkeypatch.setattr(mbox, "_imap_sync_attempt", attempt)
    notes: list[str] = []
    uids = mbox._sync_one_list(
        "netconf", "netconf", 12, Verbosity.STATUS, note_fn=notes.append
    )
    return uids, notes


def test_success_reports_count(monkeypatch, capsys):
    uids, notes = _run(monkeypatch, lambda *a: (["1", "2", "3"], 1, None))
    assert uids == ["1", "2", "3"]
    expected = "Mailing list 'netconf': 3 message(s) in the last 12 month(s) (1 new)."
    assert expected in capsys.readouterr().err
    assert notes == [expected]


def test_empty_folder_points_at_the_list_name(monkeypatch, capsys):
    # No freshness (folder truly empty) → nothing anywhere, so the list name is
    # the thing worth checking.
    uids, notes = _run(monkeypatch, lambda *a: ([], 0, None))
    assert uids == []
    err = capsys.readouterr().err
    assert "[WARN]" in err
    assert "Mailing list 'netconf': no messages in the last 12 month(s)" in err
    assert "the archive folder is empty" in err
    assert "Check the list name" in err
    assert len(notes) == 1 and "the archive folder is empty" in notes[0]


def test_empty_window_reports_holdings_and_newest(monkeypatch, capsys):
    # Folder holds mail but the newest message predates the window by years —
    # the moq case. Report the counts and let the reader draw the conclusion;
    # we don't assert "stale mirror" on the user's behalf.
    stale = datetime(2024, 6, 17)
    uids, notes = _run(monkeypatch, lambda *a: ([], 0, mbox._FolderFreshness(1333, stale)))
    assert uids == []
    err = capsys.readouterr().err
    assert "[WARN]" in err
    expected = (
        "Mailing list 'netconf': no messages in the last 12 month(s); the "
        "archive folder holds 1333 message(s), newest 2024-06-17."
    )
    assert expected in err
    assert notes == [expected]


def test_empty_window_just_past_the_edge_reads_the_same(monkeypatch, capsys):
    # A list that simply went quiet at the window edge gets the same shape of
    # message as a stalled feed — the dates distinguish them, not our guess.
    edge = datetime.now() - timedelta(days=30 * 12 + 10)
    uids, notes = _run(monkeypatch, lambda *a: ([], 0, mbox._FolderFreshness(42, edge)))
    assert uids == []
    err = capsys.readouterr().err
    assert "42 message(s)" in err
    assert f"newest {edge.strftime('%Y-%m-%d')}" in err
    assert "mirror" not in err  # no speculation about the cause
    assert len(notes) == 1


def test_folder_select_error_not_retried(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        raise mbox._FolderSelectError("netconf")

    uids, notes = _run(monkeypatch, attempt)
    assert uids == []
    assert calls["n"] == 1  # not retried
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "Mailing list 'netconf': no such folder on the IETF IMAP server" in err
    assert len(notes) == 1 and "no such folder" in notes[0]


def test_transient_error_retried_then_succeeds(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise imaplib.IMAP4.error("connection reset")
        return (["7"], 1, None)

    uids, notes = _run(monkeypatch, attempt)
    assert uids == ["7"]
    assert calls["n"] == 2  # one retry
    err = capsys.readouterr().err
    assert "[WARN]" in err and "retrying" in err
    assert "Mailing list 'netconf': 1 message(s)" in err
    # The retry itself is log-only noise; only the outcome becomes a note.
    assert len(notes) == 1 and "1 message(s)" in notes[0]


def test_transient_error_exhausts_retries(monkeypatch, capsys):
    calls = {"n": 0}

    def attempt(*_a):
        calls["n"] += 1
        raise OSError("timed out")

    uids, notes = _run(monkeypatch, attempt)
    assert uids == []
    assert calls["n"] == mbox.IMAP_RETRIES + 1
    err = capsys.readouterr().err
    assert "[ERROR]" in err
    assert "IMAP sync failed after 2 attempts (timed out)" in err
    assert len(notes) == 1 and "no mail gathered this run" in notes[0]


def test_no_list_configured_is_noted(monkeypatch, tmp_path):
    # Auto-discovery found nothing and no --mailing-list was given: the caller
    # should still learn that mail was skipped rather than infer it from absence.
    monkeypatch.setattr(mbox, "get_mailing_list_name", lambda _wg: None)
    notes: list[str] = []
    assert (
        mbox.sync_mailing_list("netconf", str(tmp_path), note_fn=notes.append) == []
    )
    assert len(notes) == 1
    assert "No mailing list configured for netconf" in notes[0]
