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
from typing import Any, Dict, List, Optional

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


# --- process_extra_drafts: batched lookup, one parallel download -----------


def _stub_bulk(
    monkeypatch: pytest.MonkeyPatch, revs: Dict[str, int]
) -> Dict[str, List[Any]]:
    """Stub the two seams process_extra_drafts drives; record both."""
    seen: Dict[str, List[Any]] = {"lookups": [], "tasks": []}

    def fake_revs(names: List[str], verbose: Any = None) -> Dict[str, int]:
        seen["lookups"].append(list(names))
        return revs

    def fake_download(tasks: List[Any], verbose: Any) -> List[str]:
        seen["tasks"].append(list(tasks))
        return [fp for _u, fp in tasks]

    monkeypatch.setattr(drafts, "fetch_current_revs", fake_revs)
    monkeypatch.setattr(drafts, "_download_files_parallel", fake_download)
    return seen


def test_extra_drafts_resolve_revisions_in_one_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One batched query, not one round-trip per draft — `--author` on a
    long-serving participant is ~130 of them."""
    names = [f"draft-ietf-wg-x{i}" for i in range(30)]
    seen = _stub_bulk(monkeypatch, {n: 2 for n in names})
    drafts.process_extra_drafts(names, str(tmp_path), Verbosity.QUIET)
    assert len(seen["lookups"]) == 1
    assert sorted(seen["lookups"][0]) == sorted(names)


def test_extra_drafts_download_in_one_parallel_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All drafts' files go to a single _download_files_parallel call, so
    the fan-out hides per-file latency instead of serialising per draft."""
    names = ["draft-a", "draft-b", "draft-c"]
    seen = _stub_bulk(monkeypatch, {n: 1 for n in names})
    drafts.process_extra_drafts(names, str(tmp_path), Verbosity.QUIET)
    assert len(seen["tasks"]) == 1
    # 3 drafts x revisions 00 and 01, full stack by default.
    assert len(seen["tasks"][0]) == 6


def test_extra_drafts_default_is_the_full_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--draft` names a specific document, where the revision history
    may well be the point — so the default must stay full."""
    seen = _stub_bulk(monkeypatch, {"draft-a": 3})
    drafts.process_extra_drafts(["draft-a"], str(tmp_path), Verbosity.QUIET)
    assert len(seen["tasks"][0]) == 4  # 00..03


def test_extra_drafts_latest_only_fetches_one_file_each(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _stub_bulk(monkeypatch, {"draft-a": 3})
    drafts.process_extra_drafts(
        ["draft-a"], str(tmp_path), Verbosity.QUIET, latest_only=True
    )
    assert [os.path.basename(fp) for _u, fp in seen["tasks"][0]] == ["draft-a-03.txt"]


def test_extra_drafts_skips_names_datatracker_does_not_know(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name absent from the batched result is skipped, exactly as a
    per-draft lookup miss was — a typo must not kill the gather."""
    seen = _stub_bulk(monkeypatch, {"draft-real": 0})
    drafts.process_extra_drafts(
        ["draft-real", "draft-typo"], str(tmp_path), Verbosity.QUIET
    )
    assert [os.path.basename(fp) for _u, fp in seen["tasks"][0]] == ["draft-real-00.txt"]


def test_extra_drafts_ignores_non_draft_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _stub_bulk(monkeypatch, {"draft-a": 0})
    drafts.process_extra_drafts(
        ["not-a-draft", "draft-a"], str(tmp_path), Verbosity.QUIET
    )
    assert seen["lookups"][0] == ["draft-a"]


def test_extra_drafts_empty_makes_no_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _stub_bulk(monkeypatch, {})
    assert drafts.process_extra_drafts([], str(tmp_path), Verbosity.QUIET) == []
    assert drafts.process_extra_drafts(["nope"], str(tmp_path), Verbosity.QUIET) == []
    assert seen["lookups"] == []
