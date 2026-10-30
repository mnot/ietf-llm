"""`fetch_wg_roles` follows Tastypie paging."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import datatracker
from ietf_llm.log import Verbosity


def _row(n: int) -> Dict[str, Any]:
    return {
        "name": "/api/v1/name/rolename/chair/",
        "email": f"/api/v1/person/email/p{n}@example.com/",
        "person": f"/api/v1/person/person/{n}/",
    }


def _stub(monkeypatch: pytest.MonkeyPatch, pages: List[Optional[Dict[str, Any]]]) -> List[str]:
    """Serve `pages` in order for the role query; record the paths asked."""
    asked: List[str] = []

    def fake_get(path: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:  # noqa: ARG001
        asked.append(path)
        return pages[len(asked) - 1]

    monkeypatch.setattr(datatracker, "_get_json", fake_get)
    monkeypatch.setattr(
        datatracker,
        "fetch_person_names",
        lambda urls: {u: f"Person {u.rstrip('/').rsplit('/', 1)[-1]}" for u in urls},
    )
    return asked


def test_roles_beyond_the_first_page_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    nxt = "/api/v1/group/role/?group__acronym=big&limit=200&offset=200"
    asked = _stub(
        monkeypatch,
        [
            {"objects": [_row(n) for n in range(1, 4)], "meta": {"next": nxt}},
            {"objects": [_row(n) for n in range(4, 6)], "meta": {"next": None}},
        ],
    )
    roles = datatracker.fetch_wg_roles("big", Verbosity.QUIET)
    assert [r.name for r in roles] == [f"Person {n}" for n in range(1, 6)]
    assert "limit=200" in asked[0] and asked[1] == nxt


def test_a_failed_later_page_is_a_failed_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    nxt = "/api/v1/group/role/?group__acronym=big&limit=200&offset=200"
    _stub(
        monkeypatch,
        [{"objects": [_row(1)], "meta": {"next": nxt}}, None],
    )
    assert datatracker.fetch_wg_roles("big", Verbosity.QUIET) == []
