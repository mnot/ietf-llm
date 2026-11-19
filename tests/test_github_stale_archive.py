"""A gh-pages `archive.json` that has fallen behind the repo is reported, and
replaced by an API fetch of the issues when a token is available.

GitHub disables a scheduled workflow after 60 days of repo inactivity and does
not re-enable it, so an i-d-template archive can stop while the repo carries on.
Without a check the corpus freezes at the archive's last run with no sign of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests

from ietf_llm.gather.sources import github
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET
ARCHIVE_URL = "https://o.github.io/r/archive.json"
ISSUES_URL = "https://api.github.com/repos/o/r/issues"


class _Resp:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status_code = status
        self._data = data

    def json(self) -> Any:
        return self._data


def _archive(exported: Optional[str]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "repo": "o/r",
        "labels": [{"name": "bug", "description": "a bug"}],
        "issues": [{"number": 1, "title": "old issue", "state": "open"}],
        "pulls": [{"number": 2, "title": "old pr", "state": "open"}],
    }
    if exported is not None:
        data["timestamp"] = exported
    return data


class _Net:
    """Stands in for `governed_get`: the archive, then the activity probe."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        archive: Dict[str, Any],
        latest: Any = "2026-02-14T00:00:00Z",
        probe: Optional[_Resp] = None,
    ) -> None:
        self.archive = archive
        self.probe = probe or _Resp(200, [{"number": 9, "updated_at": latest}])
        self.probes = 0
        monkeypatch.setattr(github, "governed_get", self.get)

    def get(self, url: str, **_kw: Any) -> _Resp:
        if url == ARCHIVE_URL:
            return _Resp(200, self.archive)
        assert url == ISSUES_URL
        self.probes += 1
        return self.probe


def _run(
    tmp_path: Path, token: Optional[str] = None
) -> "tuple[bool, Dict[str, Any], List[str]]":
    notes: List[str] = []
    dest = tmp_path / "o-r.json"
    ok = github.download_github_issues(
        "o/r", str(dest), token=token, verbose=Q, note_fn=notes.append
    )
    return ok, json.loads(dest.read_text()), notes


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_a_current_archive_is_kept_without_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = _Net(monkeypatch, _archive("2026-02-14T00:00:00+00:00"), "2026-02-15T00:00:00Z")
    ok, saved, notes = _run(tmp_path)
    assert ok and notes == []
    assert net.probes == 1
    assert [i["title"] for i in saved["issues"]] == ["old issue"]


def test_a_stale_archive_without_a_token_is_kept_and_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(
        github, "_fetch_all_issues", lambda *a, **k: pytest.fail("API used without a token")
    )
    ok, saved, notes = _run(tmp_path)
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["old issue"]
    assert len(notes) == 1
    assert "o/r" in notes[0] and "2025-11-09" in notes[0] and "2026-02-14" in notes[0]
    assert "GITHUB_TOKEN" in notes[0]


def test_a_stale_archive_with_a_token_refetches_issues_and_keeps_pulls_and_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    seen: Dict[str, Any] = {}

    def fetch(owner: str, repo: str, headers: Dict[str, str], *a: Any, **k: Any) -> Any:
        seen["auth"] = headers.get("Authorization")
        return [{"number": 1, "title": "fresh issue", "state": "open"}]

    monkeypatch.setattr(github, "_fetch_all_issues", fetch)
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok
    assert seen["auth"] == "token tok"
    assert [i["title"] for i in saved["issues"]] == ["fresh issue"]
    assert [p["title"] for p in saved["pulls"]] == ["old pr"]
    assert saved["labels"][0]["name"] == "bug"
    assert len(notes) == 1 and "2025-11-09" in notes[0] and "PRs" in notes[0]


def _boom(*a: Any, **k: Any) -> Any:
    raise requests.ConnectionError("down")


def _cached(tmp_path: Path, exported: str, title: str, **extra: Any) -> Path:
    dest = tmp_path / "o-r.json"
    dest.write_text(
        json.dumps(
            {
                "repo": "o/r",
                "timestamp": exported,
                "issues": [{"number": 1, "title": title, "state": "open"}],
                **extra,
            }
        )
    )
    return dest


def test_a_failed_api_refetch_with_nothing_cached_keeps_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(github, "_fetch_all_issues", _boom)
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["old issue"]
    assert len(notes) == 1 and "re-fetch failed; kept the archive" in notes[0]
    assert "re-fetched via" not in notes[0]


def test_a_failed_api_refetch_does_not_overwrite_a_fresher_cached_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(github, "_fetch_all_issues", _boom)
    # Fetched after the repo's last activity, so current, but marked partial so
    # that it is retried rather than skipped.
    _cached(tmp_path, "2026-03-01T00:00:00+00:00", "cached api", partial=True)
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["cached api"]
    assert len(notes) == 1 and "kept the cached copy from 2026-03-01" in notes[0]


def test_a_failed_api_refetch_replaces_a_cache_older_than_the_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(github, "_fetch_all_issues", _boom)
    _cached(tmp_path, "2025-06-01T00:00:00+00:00", "older archive")
    ok, saved, _notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["old issue"]


def test_a_cached_copy_newer_than_the_repo_activity_is_not_refetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(
        github, "_fetch_all_issues", lambda *a, **k: pytest.fail("refetched a current copy")
    )
    _cached(tmp_path, "2026-03-01T00:00:00+00:00", "cached api")
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["cached api"]
    assert len(notes) == 1 and "2025-11-09" in notes[0] and "is current" in notes[0]


def test_a_cached_copy_older_than_the_repo_activity_is_refetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    monkeypatch.setattr(
        github, "_fetch_all_issues", lambda *a, **k: [{"number": 1, "title": "fresh"}]
    )
    _cached(tmp_path, "2026-01-01T00:00:00+00:00", "cached api")
    ok, saved, _notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["fresh"]
    assert "partial" not in saved


def test_a_refetch_with_issues_left_at_cached_copies_is_marked_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))

    def fetch(*a: Any, report: Any, **k: Any) -> Any:
        report.add(1, "HTTP 500", True)
        return [{"number": 1, "title": "kept"}]

    monkeypatch.setattr(github, "_fetch_all_issues", fetch)
    ok, saved, _notes = _run(tmp_path, token="tok")
    assert ok and saved["partial"] is True


def test_an_archive_within_the_grace_period_is_not_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2026-02-04T00:00:00+00:00"))  # probe: 2026-02-14
    ok, _saved, notes = _run(tmp_path, token="tok")
    assert ok and notes == []


@pytest.mark.parametrize(
    "probe",
    [_Resp(403), _Resp(200, []), _Resp(200, {"message": "x"}), _Resp(200, [{"number": 1}])],
)
def test_an_activity_check_that_cannot_be_made_is_not_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe: _Resp
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"), probe=probe)
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok and notes == []
    assert [i["title"] for i in saved["issues"]] == ["old issue"]


def test_an_activity_check_that_raises_is_not_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def get(url: str, **_kw: Any) -> _Resp:
        if url == ARCHIVE_URL:
            return _Resp(200, _archive("2025-11-09T00:00:00+00:00"))
        raise requests.ConnectionError("down")

    monkeypatch.setattr(github, "governed_get", get)
    ok, saved, notes = _run(tmp_path)
    assert ok and notes == []


def test_an_archive_with_no_usable_timestamp_is_not_probed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = _Net(monkeypatch, _archive(None))
    ok, _saved, notes = _run(tmp_path)
    assert ok and notes == [] and net.probes == 0


def test_download_github_archives_forwards_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))
    notes: List[str] = []
    pending = github.download_github_archives(
        ["o/r"], str(tmp_path), Q, suppress_raw=True, note_fn=notes.append
    )
    assert pending == [] and len(notes) == 1
