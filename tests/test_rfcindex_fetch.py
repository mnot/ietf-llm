"""Discovery and download of rfc.fyi's published index (#230).

The network is stubbed: these tests are about which release we select and
what we refuse, not about GitHub. The tarball is built here so the extract
and the tag/manifest cross-check run for real.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from typing import Any, Dict, List

import pytest

from ietf_llm.rfcindex import fetch
from ietf_llm.rfcindex.format import RfcIndexError

from test_rfcindex_format import DIMS, _manifest, _write_index  # noqa: F401


class _FakeResponse:
    def __init__(self, status: int, payload: Any = None, content: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        return self._payload


def _entry(tag: str, **over: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": fetch.ASSET_NAME,
                "size": 1234,
                "browser_download_url": f"https://example.invalid/{tag}.tar.gz",
            }
        ],
    }
    item.update(over)
    return item


def _stub_list(monkeypatch: pytest.MonkeyPatch, entries: List[Dict[str, Any]]) -> None:
    monkeypatch.setattr(
        fetch, "governed_get", lambda url, **kw: _FakeResponse(200, entries)
    )


def test_picks_the_highest_build_not_the_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deliberately out of order: selection must not trust the listing.
    _stub_list(
        monkeypatch,
        [
            _entry("index-20260601T000000Z"),
            _entry("index-20261101T000000Z"),
            _entry("index-20260811T003915Z"),
        ],
    )
    release = fetch.latest_release("owner/repo")
    assert release is not None
    assert release.tag == "index-20261101T000000Z"
    assert release.build == "20261101T000000Z"


def test_ignores_drafts_prereleases_and_foreign_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_list(
        monkeypatch,
        [
            _entry("index-20261201T000000Z", draft=True),
            _entry("index-20261101T000000Z", prerelease=True),
            _entry("v3.1.0"),
            _entry("index-20260811T003915Z"),
        ],
    )
    release = fetch.latest_release("owner/repo")
    assert release is not None
    assert release.tag == "index-20260811T003915Z"


def test_release_without_the_asset_is_not_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_list(
        monkeypatch,
        [
            _entry("index-20261101T000000Z", assets=[{"name": "notes.txt"}]),
            _entry("index-20260811T003915Z"),
        ],
    )
    release = fetch.latest_release("owner/repo")
    assert release is not None
    assert release.build == "20260811T003915Z"


def test_no_index_release_is_none_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_list(monkeypatch, [_entry("v1.0.0")])
    assert fetch.latest_release("owner/repo") is None


def test_api_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetch, "governed_get", lambda url, **kw: _FakeResponse(503, None)
    )
    with pytest.raises(RfcIndexError, match="HTTP 503"):
        fetch.latest_release("owner/repo")


def _tarball(tmp_path: Any, build: str) -> bytes:
    """A real `index.tar.gz`: an index tree rooted at `index/`."""
    staging = tmp_path / f"stage-{build}"
    doc = _manifest(build=build)
    _write_index(str(staging), doc)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(staging), arcname=fetch.ARCHIVE_ROOT)
    return buf.getvalue()


def _stub_asset(monkeypatch: pytest.MonkeyPatch, blob: bytes) -> None:
    monkeypatch.setattr(
        fetch, "governed_get", lambda url, **kw: _FakeResponse(200, None, blob)
    )


def test_downloads_and_verifies(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    build = "20260811T003915Z"
    _stub_asset(monkeypatch, _tarball(tmp_path, build))
    release = fetch.IndexRelease(
        tag=f"index-{build}", build=build, asset_url="x", asset_bytes=10
    )
    index_dir = fetch.download_index(release, str(tmp_path / "out"))
    assert os.path.basename(index_dir) == fetch.ARCHIVE_ROOT
    assert fetch.manifest_for(index_dir).build == build
    # The tarball itself is not left behind.
    assert not [f for f in os.listdir(tmp_path / "out") if f.endswith(".tar.gz")]


def test_tag_and_manifest_build_must_agree(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_asset(monkeypatch, _tarball(tmp_path, "20260811T003915Z"))
    release = fetch.IndexRelease(
        tag="index-20990101T000000Z",
        build="20990101T000000Z",
        asset_url="x",
        asset_bytes=10,
    )
    with pytest.raises(RfcIndexError, match="the tag says"):
        fetch.download_index(release, str(tmp_path / "out"))


def test_path_escaping_member_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = json.dumps({"pwned": True}).encode("utf-8")
        info = tarfile.TarInfo("../escaped.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    _stub_asset(monkeypatch, buf.getvalue())
    release = fetch.IndexRelease(
        tag="index-20260811T003915Z",
        build="20260811T003915Z",
        asset_url="x",
        asset_bytes=10,
    )
    with pytest.raises(RfcIndexError, match="escapes destination"):
        fetch.download_index(release, str(tmp_path / "out"))
    assert not os.path.exists(tmp_path / "escaped.json")
