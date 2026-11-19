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


def test_a_failed_api_refetch_keeps_the_stale_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Net(monkeypatch, _archive("2025-11-09T00:00:00+00:00"))

    def boom(*a: Any, **k: Any) -> Any:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(github, "_fetch_all_issues", boom)
    ok, saved, notes = _run(tmp_path, token="tok")
    assert ok
    assert [i["title"] for i in saved["issues"]] == ["old issue"]
    assert len(notes) == 1


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
