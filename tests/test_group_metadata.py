"""Tests for Datatracker group-metadata helpers in `datatracker_api`.

`get_mailing_list_name` / `get_group_resources` read the group record
and its Additional Resources from the API. We stub `fetch_resource`
(and clear the lru_caches) so no HTTP is hit. The load-bearing case is
the httpbis-style off-IETF list: the primary `list_email` is at w3.org,
so the IMAP archive name must come from the `mailing_list_archive`
Additional Resource (`httpbisa`), not the list_email local part.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm import datatracker_api, utils


class _FakeResp:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}

    def json(self) -> Dict[str, Any]:
        return self._payload


def _stub_api(
    monkeypatch: pytest.MonkeyPatch,
    group: Optional[Dict[str, Any]],
    resources: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Stub datatracker_api.fetch_resource: group record by acronym, extresources
    by group id. Clears the per-process caches first."""
    datatracker_api.fetch_group_object.cache_clear()
    datatracker_api.get_group_resources.cache_clear()

    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResp]:
        if "/group/group/" in url:
            return _FakeResp({"objects": [group] if group else []})
        if "/group/groupextresource/" in url:
            return _FakeResp({"objects": resources or []})
        return None

    monkeypatch.setattr(datatracker_api, "fetch_resource", fake_fetch)


def test_mailing_list_name_primary_for_ietf_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(monkeypatch, {"id": 1, "list_email": "tls@ietf.org"})
    # An ietf.org list uses its local part directly; no resource lookup.
    assert datatracker_api.get_mailing_list_name("tls") == "tls"


def test_mailing_list_name_falls_back_to_alternate_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # httpbis: list hosted at w3.org, IETF mirror named `httpbisa`.
    _stub_api(
        monkeypatch,
        {"id": 1718, "list_email": "ietf-http-wg@w3.org"},
        [
            {
                "name": "/api/v1/name/extresourcename/mailing_list_archive/",
                "value": "https://mailarchive.ietf.org/arch/browse/httpbisa/",
            },
            {
                "name": "/api/v1/name/extresourcename/github_org/",
                "value": "https://github.com/httpwg/",
            },
        ],
    )
    assert datatracker_api.get_mailing_list_name("httpbis") == "httpbisa"


def test_mailing_list_name_external_without_alternate_keeps_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Off-IETF list but no mailing_list_archive resource → best-effort
    # primary local part (mail sync will just find nothing, gracefully).
    _stub_api(monkeypatch, {"id": 2, "list_email": "some-list@example.com"}, [])
    assert datatracker_api.get_mailing_list_name("foo") == "some-list"


def test_mailing_list_name_no_record_uses_shortname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(monkeypatch, None)
    assert datatracker_api.get_mailing_list_name("ghostwg") == "ghostwg"


def test_group_resources_parses_slug_label_and_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(
        monkeypatch,
        {"id": 1718, "list_email": "ietf-http-wg@w3.org"},
        [
            {
                "name": "/api/v1/name/extresourcename/webpage/",
                "display_name": "home page",
                "value": "https://httpwg.org/",
            },
            {
                "name": "/api/v1/name/extresourcename/zulip/",
                # No display_name → falls back to slug.
                "value": "https://zulip.ietf.org/#narrow/stream/225-httpbis",
            },
        ],
    )
    by_slug = {slug: (label, value) for slug, label, value in
               datatracker_api.get_group_resources("httpbis")}
    assert by_slug["webpage"] == ("home page", "https://httpwg.org/")
    assert by_slug["zulip"][0] == "zulip"  # slug fallback
    assert by_slug["zulip"][1].endswith("225-httpbis")


def test_group_state_and_area(monkeypatch: pytest.MonkeyPatch) -> None:
    # State comes off the group record; area resolves the parent link.
    datatracker_api.fetch_group_object.cache_clear()

    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResp]:
        if "/group/group/?acronym=" in url:
            return _FakeResp({"objects": [{
                "id": 1718,
                "state": "/api/v1/name/groupstatename/active/",
                "parent": "/api/v1/group/group/2412/",
            }]})
        if "/group/group/2412/" in url:
            return _FakeResp({"acronym": "wit", "name": "Web and Internet Transport"})
        return None

    monkeypatch.setattr(datatracker_api, "fetch_resource", fake_fetch)
    assert datatracker_api.get_group_state("httpbis") == "active"
    assert datatracker_api.get_group_area("httpbis") == ("wit", "Web and Internet Transport")


def test_group_name(monkeypatch: pytest.MonkeyPatch) -> None:
    datatracker_api.fetch_group_object.cache_clear()

    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResp]:
        if "/group/group/?acronym=" in url:
            return _FakeResp({"objects": [{"id": 1718, "name": "HTTP"}]})
        return None

    monkeypatch.setattr(datatracker_api, "fetch_resource", fake_fetch)
    assert datatracker_api.get_group_name("httpbis") == "HTTP"
    datatracker_api.fetch_group_object.cache_clear()
    monkeypatch.setattr(datatracker_api, "fetch_resource", lambda *a, **k: None)
    assert datatracker_api.get_group_name("nope") is None


# --- group.md writer + overview surfacing --------------------------------


def test_write_group_info_renders_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ietf_llm.gather.sources import group_info
    from ietf_llm.paths import group_path

    monkeypatch.setattr(group_info, "get_group_name", lambda wg: "HTTP")
    monkeypatch.setattr(group_info, "get_group_state", lambda wg: "active")
    monkeypatch.setattr(
        group_info, "get_group_area", lambda wg: ("wit", "Web and Internet Transport")
    )
    monkeypatch.setattr(
        group_info, "get_group_resources",
        lambda wg: (("github_org", "repositories", "https://github.com/httpwg/"),),
    )
    written = group_info.write_group_info("httpbis", str(tmp_path), utils.Verbosity.QUIET)
    assert written
    text = open(group_path(str(tmp_path)), encoding="utf-8").read()
    assert "**Name:** HTTP" in text
    assert "**Status:** active" in text
    assert "**Area:** Web and Internet Transport (wit)" in text
    assert "- repositories: https://github.com/httpwg/" in text


def test_write_group_info_noop_when_empty(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ietf_llm.gather.sources import group_info
    from ietf_llm.paths import group_path

    monkeypatch.setattr(group_info, "get_group_name", lambda wg: None)
    monkeypatch.setattr(group_info, "get_group_state", lambda wg: None)
    monkeypatch.setattr(group_info, "get_group_area", lambda wg: None)
    monkeypatch.setattr(group_info, "get_group_resources", lambda wg: ())
    assert group_info.write_group_info("x-foo", str(tmp_path), utils.Verbosity.QUIET) == []
    assert not os.path.exists(group_path(str(tmp_path)))


def test_overview_surfaces_group_facts(tmp_path: Any) -> None:
    from ietf_llm.digest.overview import build_overview
    from ietf_llm.paths import group_path

    # A cache with just group.md still surfaces status / area / resources.
    gpath = group_path(str(tmp_path))
    with open(gpath, "w", encoding="utf-8") as fh:
        fh.write(
            "# httpbis — working group metadata\n\n"
            "**Status:** active\n"
            "**Area:** Web and Internet Transport (wit)\n\n"
            "## Resources\n"
            "- home page: https://httpwg.org/\n"
        )
    out = build_overview("httpbis", str(tmp_path))
    assert "**Status:** active" in out
    assert "**Area:** Web and Internet Transport (wit)" in out
    assert "## Resources" in out
    assert "- home page: https://httpwg.org/" in out
