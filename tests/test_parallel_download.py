"""The parallel document downloader in `gather.sources.drafts`.

Two invariants matter: every task's file is written, and every worker thread's
request is still counted in the gather's egress total. The accumulator is
thread-local, so the workers binding to the parent's accumulator (via
`http_metrics.set_current`) and `record` being locked are what make the count
correct under concurrency -- this guards both.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import ietf_llm.net.http_metrics as http_metrics
from ietf_llm.gather.sources import drafts
from ietf_llm.log import Verbosity


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def _fake_fetch_resource(
    url: str, headers: Optional[Dict[str, str]] = None
) -> _Resp:
    # Mimic the real fetch_resource: record one egress hit, return a body.
    http_metrics.record(url, 200, len(url))
    return _Resp(f"body for {url}")


def test_parallel_download_writes_every_file_and_counts_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(drafts, "fetch_resource", _fake_fetch_resource)
    http_metrics.reset()

    count = 50
    tasks = [
        (
            f"https://www.ietf.org/archive/id/draft-x-{i:02d}.txt",
            str(tmp_path / f"draft-x-{i:02d}.txt"),
        )
        for i in range(count)
    ]
    written = drafts._download_files_parallel(tasks, Verbosity.QUIET)

    assert len(written) == count
    for _url, filepath in tasks:
        assert os.path.exists(filepath)
    # Every worker's request landed in the parent accumulator, with no lost
    # updates from the concurrent record() calls.
    metrics = http_metrics.current()
    assert metrics.total == count
    assert metrics.ok == count


def test_parallel_download_empty_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    http_metrics.reset()
    assert drafts._download_files_parallel([], Verbosity.QUIET) == []
    assert http_metrics.current().total == 0


def test_parallel_download_skips_failed_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fetch that returns None (the failure signal) must not produce a file
    # or a written-path entry, but the others still succeed.
    def flaky(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
        http_metrics.record(url, 200 if "ok" in url else 404, 0, error="ok" not in url)
        return _Resp("body") if "ok" in url else None

    monkeypatch.setattr(drafts, "fetch_resource", flaky)
    http_metrics.reset()
    tasks = [
        ("https://h/ok-1.txt", str(tmp_path / "ok-1.txt")),
        ("https://h/bad-1.txt", str(tmp_path / "bad-1.txt")),
        ("https://h/ok-2.txt", str(tmp_path / "ok-2.txt")),
    ]
    written = drafts._download_files_parallel(tasks, Verbosity.QUIET)
    assert sorted(os.path.basename(p) for p in written) == ["ok-1.txt", "ok-2.txt"]
    assert not os.path.exists(tmp_path / "bad-1.txt")


def test_revision_tasks_latest_only_vs_full(tmp_path: Path) -> None:
    # A WG gather fetches only the current revision; the full-stack form
    # (used by `--draft`) enumerates every revision 00..max_rev. Either way,
    # an already-cached revision is skipped.
    out = str(tmp_path)
    latest = drafts._revision_tasks("draft-ietf-wg-x", 5, out, latest_only=True)
    assert [os.path.basename(fp) for _u, fp in latest] == ["draft-ietf-wg-x-05.txt"]

    full = drafts._revision_tasks("draft-ietf-wg-x", 2, out)
    assert [os.path.basename(fp) for _u, fp in full] == [
        "draft-ietf-wg-x-00.txt",
        "draft-ietf-wg-x-01.txt",
        "draft-ietf-wg-x-02.txt",
    ]

    (tmp_path / "draft-ietf-wg-x-05.txt").write_text("cached")
    assert drafts._revision_tasks("draft-ietf-wg-x", 5, out, latest_only=True) == []


def test_download_one_failed_write_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write that dies partway must not leave a truncated draft under its
    # final name: the revision loop would treat it as cached forever.
    class _Boom:
        @property
        def text(self) -> str:
            raise OSError("disk full")

    monkeypatch.setattr(drafts, "fetch_resource", lambda url, headers=None: _Boom())
    target = tmp_path / "draft-x-01.txt"
    assert drafts._download_one("https://h/x", str(target), Verbosity.QUIET) is None
    assert os.listdir(tmp_path) == []

    class _Partial:
        text = "complete body"

    real_replace = os.replace

    def dying_replace(src: str, dst: str) -> None:
        raise OSError("killed before rename")

    monkeypatch.setattr(drafts, "fetch_resource", lambda url, headers=None: _Partial())
    monkeypatch.setattr("ietf_llm.atomicio.os.replace", dying_replace)
    assert drafts._download_one("https://h/x", str(target), Verbosity.QUIET) is None
    monkeypatch.setattr("ietf_llm.atomicio.os.replace", real_replace)
    assert os.listdir(tmp_path) == []


def test_zero_length_existing_file_is_refetched(tmp_path: Path) -> None:
    out = str(tmp_path)
    (tmp_path / "draft-ietf-wg-x-05.txt").write_text("")
    tasks = drafts._revision_tasks("draft-ietf-wg-x", 5, out, latest_only=True)
    assert [os.path.basename(fp) for _u, fp in tasks] == ["draft-ietf-wg-x-05.txt"]
    (tmp_path / "draft-ietf-wg-x-05.txt").write_text("content")
    assert drafts._revision_tasks("draft-ietf-wg-x", 5, out, latest_only=True) == []
