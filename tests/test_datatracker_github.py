"""Tests for the Datatracker `github_username` resolver
(ietf_llm.gather.sources.datatracker_github).

Covers the contract:
- build the global github_username -> person index, then resolve a login
  to its real name + verified emails
- logins absent from the index are cached as confirmed misses
- the index and per-login results are cached, so a second pass makes no
  further requests
- a transient failure (None from _get_json) is not cached
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

import ietf_llm.gather.sources.datatracker_github as dtg
from ietf_llm.utils import Verbosity, get_cache_dir


def _cache_file() -> Path:
    return Path(get_cache_dir()) / "_datatracker-github.json"


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    index: List[Tuple[str, str]],
    persons: Optional[dict] = None,
    emails: Optional[dict] = None,
    calls: Optional[List[str]] = None,
) -> None:
    """Install a fake `_get_json` returning canned index / person / email
    bodies. `index` is a list of (login, person_uri); `persons` maps
    person_uri -> name; `emails` maps person_id -> [addresses]."""
    persons = persons or {}
    emails = emails or {}

    def fake(path_or_url: str, timeout: float = 10.0) -> Optional[dict[str, Any]]:  # noqa: ARG001
        if calls is not None:
            calls.append(path_or_url)
        if "personextresource" in path_or_url:
            return {
                "objects": [
                    {"value": login, "person": uri} for login, uri in index
                ],
                "meta": {"next": None},
            }
        if "/email/" in path_or_url:
            person_id = path_or_url.split("person=", 1)[1].split("&", 1)[0]
            return {
                "objects": [{"address": a} for a in emails.get(person_id, [])]
            }
        for uri, name in persons.items():
            if path_or_url.endswith(uri) or path_or_url == uri:
                return {"name": name}
        return None

    monkeypatch.setattr(dtg, "_get_json", fake)


def test_resolves_login_to_name_and_emails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/125125/")],
        persons={"/api/v1/person/person/125125/": "Roberto Polli"},
        emails={"125125": ["robipolli@gmail.com"]},
    )
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert "ioggstream" in out
    assert out["ioggstream"].name == "Roberto Polli"
    assert out["ioggstream"].emails == ["robipolli@gmail.com"]


def test_match_is_case_insensitive(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/1/")],
        persons={"/api/v1/person/person/1/": "Roberto Polli"},
        emails={"1": ["robipolli@gmail.com"]},
    )
    out = dtg.resolve_via_datatracker(["IoggStream"], verbose=Verbosity.QUIET)
    assert "IoggStream" in out
    assert out["IoggStream"].name == "Roberto Polli"


def test_login_not_in_index_is_cached_miss(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, index=[("someoneelse", "/api/v1/person/person/9/")])
    out = dtg.resolve_via_datatracker(["nobody"], verbose=Verbosity.QUIET)
    assert out == {}
    cache = json.loads(_cache_file().read_text())
    assert cache["nobody"] is None


def test_second_pass_makes_no_requests(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/1/")],
        persons={"/api/v1/person/person/1/": "Roberto Polli"},
        emails={"1": ["robipolli@gmail.com"]},
        calls=calls,
    )
    dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    first = len(calls)
    assert first > 0
    # Everything (index + per-login) is now cached on disk.
    dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert len(calls) == first


def test_transient_failure_not_cached(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Index resolves the login, but the per-person fetch fails (None).
    # The login must NOT be recorded, so the next run retries.
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/1/")],
        persons={},  # person fetch returns None → transient
        emails={},
    )
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert out == {}
    cache = json.loads(_cache_file().read_text())
    assert "ioggstream" not in cache  # not cached → retried next run


def test_index_build_failure_returns_empty(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dtg, "_get_json", lambda path, timeout=10.0: None)
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert out == {}
    # No index written, so a later (working) run can still build it.
    assert not _cache_file().exists()
