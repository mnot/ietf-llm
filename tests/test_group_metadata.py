"""Tests for Datatracker group-metadata helpers in `utils`.

`get_mailing_list_name` / `get_group_resources` read the group record
and its Additional Resources from the API. We stub `fetch_resource`
(and clear the lru_caches) so no HTTP is hit. The load-bearing case is
the httpbis-style off-IETF list: the primary `list_email` is at w3.org,
so the IMAP archive name must come from the `mailing_list_archive`
Additional Resource (`httpbisa`), not the list_email local part.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from ietf_llm import utils


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
    """Stub utils.fetch_resource: group record by acronym, extresources
    by group id. Clears the per-process caches first."""
    utils._fetch_group_object.cache_clear()
    utils.get_group_resources.cache_clear()

    def fake_fetch(
        url: str, headers: Optional[Dict[str, str]] = None,  # noqa: ARG001
    ) -> Optional[_FakeResp]:
        if "/group/group/" in url:
            return _FakeResp({"objects": [group] if group else []})
        if "/group/groupextresource/" in url:
            return _FakeResp({"objects": resources or []})
        return None

    monkeypatch.setattr(utils, "fetch_resource", fake_fetch)


def test_mailing_list_name_primary_for_ietf_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(monkeypatch, {"id": 1, "list_email": "tls@ietf.org"})
    # An ietf.org list uses its local part directly; no resource lookup.
    assert utils.get_mailing_list_name("tls") == "tls"


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
    assert utils.get_mailing_list_name("httpbis") == "httpbisa"


def test_mailing_list_name_external_without_alternate_keeps_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Off-IETF list but no mailing_list_archive resource → best-effort
    # primary local part (mail sync will just find nothing, gracefully).
    _stub_api(monkeypatch, {"id": 2, "list_email": "some-list@example.com"}, [])
    assert utils.get_mailing_list_name("foo") == "some-list"


def test_mailing_list_name_no_record_uses_shortname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(monkeypatch, None)
    assert utils.get_mailing_list_name("ghostwg") == "ghostwg"


def test_group_resources_parses_slug_and_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_api(
        monkeypatch,
        {"id": 1718, "list_email": "ietf-http-wg@w3.org"},
        [
            {
                "name": "/api/v1/name/extresourcename/webpage/",
                "value": "https://httpwg.org/",
            },
            {
                "name": "/api/v1/name/extresourcename/zulip/",
                "value": "https://zulip.ietf.org/#narrow/stream/225-httpbis",
            },
        ],
    )
    resources = dict(utils.get_group_resources("httpbis"))
    assert resources["webpage"] == "https://httpwg.org/"
    assert resources["zulip"].endswith("225-httpbis")
