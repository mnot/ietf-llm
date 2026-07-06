"""Tests for author-spec resolution (email / id / name)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from ietf_llm.gather.sources import author
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


def _stub(monkeypatch: pytest.MonkeyPatch, responses: Dict[str, Any]) -> None:
    """Stub author._get_json with a URL-substring lookup."""
    def fake(url: str, timeout: float = 10.0) -> Optional[Any]:  # noqa: ARG001
        for key, body in responses.items():
            if key in url:
                return body
        return None

    monkeypatch.setattr(author, "_get_json", fake)


def test_resolve_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {
        "/person/email/mnot@mnot.net/": {
            "person": "/api/v1/person/person/103881/"
        },
        "/person/person/103881/": {"name": "Mark Nottingham"},
    })
    assert author.resolve_person("mnot@mnot.net", Q) == (103881, "Mark Nottingham")


def test_resolve_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"/person/person/103881/": {"name": "Mark Nottingham"}})
    assert author.resolve_person("103881", Q) == (103881, "Mark Nottingham")


def test_resolve_by_exact_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {
        "name__icontains=": {"objects": [
            {"id": 1, "name": "Mark Nottingham"},
            {"id": 2, "name": "Mark Notting-Other"},
        ]},
    })
    # Exact (case-insensitive) match wins over the substring sibling.
    assert author.resolve_person("mark nottingham", Q) == (1, "Mark Nottingham")


def test_resolve_ambiguous_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, {
        "name__icontains=": {"objects": [
            {"id": 1, "name": "Mark Smith"},
            {"id": 2, "name": "Mark Jones"},
        ]},
    })
    assert author.resolve_person("Mark", Q) is None


def test_resolve_unknown_email_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {})  # email endpoint returns None
    assert author.resolve_person("nobody@nowhere.invalid", Q) is None
