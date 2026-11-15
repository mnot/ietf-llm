"""A failed comment fetch must not truncate an issue in the archive.

`download_github_issues` falls back to the API when a repo publishes no
gh-pages archive. If paging one issue's comments fails (a throttle, a 5xx, a
dropped connection) that issue keeps its previously cached copy rather than
being written with a partial discussion, the other issues still update, and
the gather says how many were left stale and why.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests

from ietf_llm.gather.sources import github
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET
BASE = "https://api.github.com/repos/o/r"


class _Resp:
    def __init__(
        self, status: int, data: Any = None, headers: Optional[Dict[str, str]] = None
    ) -> None:
        self.status_code = status
        self._data = data
        self.headers = headers or {}

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _api_issue(number: int, comments: int) -> Dict[str, Any]:
    return {
        "number": number,
        "title": f"fresh {number}",
        "state": "open",
        "user": {"login": "alice"},
        "created_at": "2026-01-01T00:00:00Z",
        "labels": [],
        "body": "b",
        "comments": comments,
        "comments_url": f"{BASE}/issues/{number}/comments",
    }


def _comment(i: int) -> Dict[str, Any]:
    return {"user": {"login": "bob"}, "created_at": "t", "body": f"fresh comment {i}"}


def _cached(number: int) -> Dict[str, Any]:
    return {
        "number": number,
        "title": f"cached {number}",
        "state": "open",
        "author": "alice",
        "createdAt": "2025-01-01T00:00:00Z",
        "labels": [],
        "body": "old",
        "comments": [{"author": "bob", "createdAt": "t", "body": "cached comment"}],
    }


class _Api:
    """Routes the fake GitHub: `listing` is page 1 of /issues; `comments` maps
    an issue number to a function (page) -> response."""

    def __init__(self, listing: List[Dict[str, Any]], comments: Dict[int, Any]) -> None:
        self.listing = listing
        self.comments = comments
        self.comment_requests: List[tuple] = []

    def __call__(self, url: str, **kwargs: Any) -> _Resp:
        if url.endswith("/archive.json"):
            return _Resp(404)
        m = re.search(r"/issues/(\d+)/comments$", url)
        if m:
            number, page = int(m.group(1)), kwargs["params"]["page"]
            self.comment_requests.append((number, page))
            return self.comments[number](page)
        assert "/issues?state=all" in url
        return _Resp(200, self.listing if "page=1&" in url else [])


def _ok_comments(n: int):
    def respond(page: int) -> _Resp:
        if page == 1:
            return _Resp(200, [_comment(i) for i in range(min(n, 100))])
        return _Resp(200, [_comment(100 + i) for i in range(n - 100)])

    return respond


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    api: _Api,
    cached: List[int],
    caplog: Any = None,
) -> Dict[int, Dict[str, Any]]:
    dest = tmp_path / "archive.json"
    dest.write_text(
        json.dumps({"repo": "o/r", "issues": [_cached(n) for n in cached]})
    )
    monkeypatch.setattr(github, "governed_get", api)
    assert github.download_github_issues("o/r", str(dest), verbose=Q)
    return {i["number"]: i for i in json.loads(dest.read_text())["issues"]}


def _logs(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    lines: List[str] = []
    monkeypatch.setattr(
        github, "log", lambda msg, *a, **k: lines.append(str(msg))
    )
    return lines


def test_clean_run_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = _logs(monkeypatch)
    api = _Api(
        [_api_issue(1, 1), _api_issue(2, 150), _api_issue(3, 0)],
        {1: _ok_comments(1), 2: _ok_comments(150)},
    )
    out = _run(monkeypatch, tmp_path, api, cached=[1, 2])
    assert sorted(out) == [1, 2, 3]
    assert out[2]["title"] == "fresh 2" and len(out[2]["comments"]) == 150
    assert out[1]["comments"][0]["body"] == "fresh comment 0"
    assert not any("not refreshed" in m for m in lines)


def test_rate_limit_on_a_later_comments_page_keeps_the_cached_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = _logs(monkeypatch)

    def second_page_throttled(page: int) -> _Resp:
        if page == 1:
            return _Resp(200, [_comment(i) for i in range(100)])
        return _Resp(429, None, {"Retry-After": "120"})

    # #2 is last in the listing, so the others have updated before it fails.
    api = _Api(
        [_api_issue(1, 1), _api_issue(3, 1), _api_issue(2, 150)],
        {1: _ok_comments(1), 3: _ok_comments(1), 2: second_page_throttled},
    )
    out = _run(monkeypatch, tmp_path, api, cached=[1, 2, 3])
    # The throttled issue is exactly its cached copy, not a 100-comment stub.
    assert out[2] == _cached(2)
    assert out[1]["title"] == "fresh 1" and out[3]["title"] == "fresh 3"
    report = [m for m in lines if "not refreshed" in m]
    assert len(report) == 1
    assert "1 issue(s)" in report[0]
    assert "rate limited (HTTP 429, Retry-After 120s, over the 60s limit)" in report[0]


def test_a_throttle_stops_the_repo_and_keeps_every_unreached_cached_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = _logs(monkeypatch)
    api = _Api(
        [_api_issue(1, 1), _api_issue(2, 1), _api_issue(3, 1), _api_issue(4, 1)],
        {
            1: lambda page: _Resp(429, None, {"Retry-After": "120"}),
            2: _ok_comments(1),
            3: _ok_comments(1),
            4: _ok_comments(1),
        },
    )
    out = _run(monkeypatch, tmp_path, api, cached=[1, 2, 3])
    # Nothing was asked for after the throttle ...
    assert api.comment_requests == [(1, 1)]
    # ... cached issues are all kept as they were, and #4 (never cached) is not
    # written with unfetched comments.
    assert out == {n: _cached(n) for n in (1, 2, 3)}
    report = [m for m in lines if "not refreshed" in m][0]
    assert "4 issue(s)" in report and "3 kept at their cached copy" in report
    assert "1 never cached so skipped" in report
    assert "stopped fetching this repo" in report


def test_other_failures_skip_that_issue_and_the_rest_still_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = _logs(monkeypatch)
    api = _Api(
        [_api_issue(1, 1), _api_issue(2, 1), _api_issue(3, 1)],
        {1: _ok_comments(1), 2: lambda page: _Resp(500), 3: _ok_comments(1)},
    )
    out = _run(monkeypatch, tmp_path, api, cached=[1, 2, 3])
    assert out[2] == _cached(2)
    assert out[1]["title"] == "fresh 1" and out[3]["title"] == "fresh 3"
    assert [r for r in api.comment_requests if r[0] == 3] == [(3, 1)]
    report = [m for m in lines if "not refreshed" in m][0]
    assert "HTTP 500" in report and "1 issue(s)" in report


def test_a_new_issue_with_failed_comments_is_not_written_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = _Api([_api_issue(1, 1), _api_issue(2, 1)], {1: _ok_comments(1), 2: lambda p: _Resp(500)})
    out = _run(monkeypatch, tmp_path, api, cached=[])
    assert sorted(out) == [1]


def test_a_dropped_connection_counts_as_a_failure_and_repeats_stop_the_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = _logs(monkeypatch)

    def down(page: int) -> _Resp:
        raise requests.ConnectionError("down")

    listing = [_api_issue(n, 1) for n in range(1, 7)]
    api = _Api(listing, {n: down for n in range(1, 7)})
    out = _run(monkeypatch, tmp_path, api, cached=[1, 2, 3, 4, 5, 6])
    assert out == {n: _cached(n) for n in range(1, 7)}
    # Three in a row and the repo is given up on: no further requests.
    assert len(api.comment_requests) == github._MAX_CONSECUTIVE_COMMENT_FAILURES
    report = [m for m in lines if "not refreshed" in m][0]
    assert "6 issue(s)" in report and "3 failures in a row" in report
