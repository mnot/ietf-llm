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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

import ietf_llm.gather.sources.datatracker_github as dtg
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_cache_dir


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


def _age_file(key: str, days: int) -> None:
    """Backdate the stamp at top-level `key` (`_index_fetched_at`) or inside
    the per-login entry `key`."""
    data = json.loads(_cache_file().read_text())
    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if key == "_index_fetched_at":
        data[key] = old
    else:
        data[key]["fetched_at"] = old
    _cache_file().write_text(json.dumps(data))


def test_stale_index_is_rebuilt_and_finds_a_new_claim(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index: List[Tuple[str, str]] = []
    calls: List[str] = []
    _stub(
        monkeypatch,
        index=index,
        persons={"/api/v1/person/person/5/": "New Claimer"},
        emails={"5": ["new@x.example"]},
        calls=calls,
    )
    assert dtg.resolve_via_datatracker(["newbie"], verbose=Verbosity.QUIET) == {}
    assert json.loads(_cache_file().read_text())["newbie"] is None
    index.append(("newbie", "/api/v1/person/person/5/"))
    # A fresh index is trusted: no rebuild, the miss stands.
    assert dtg.resolve_via_datatracker(["newbie"], verbose=Verbosity.QUIET) == {}
    assert len(calls) == 1
    # Once it is old, the index is rebuilt and the recorded miss is rechecked.
    _age_file("_index_fetched_at", 31)
    out = dtg.resolve_via_datatracker(["newbie"], verbose=Verbosity.QUIET)
    assert out["newbie"].emails == ["new@x.example"]
    assert json.loads(_cache_file().read_text())["newbie"]["name"] == "New Claimer"


def test_index_without_stamp_is_rebuilt(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cache_file().parent.mkdir(parents=True, exist_ok=True)
    _cache_file().write_text(json.dumps({"_index": {}}))
    calls: List[str] = []
    _stub(monkeypatch, index=[("a", "/api/v1/person/person/1/")], calls=calls)
    dtg.resolve_via_datatracker(["a"], verbose=Verbosity.QUIET)
    assert any("personextresource" in c for c in calls)


def test_stale_index_is_kept_when_the_rebuild_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, index=[("ioggstream", "/api/v1/person/person/1/")])
    dtg.resolve_via_datatracker(["other"], verbose=Verbosity.QUIET)
    _age_file("_index_fetched_at", 60)
    monkeypatch.setattr(dtg, "_get_json", lambda path, timeout=10.0: None)
    # The rebuild fails at the first page; the old index still answers rather
    # than the whole step giving up.
    dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    cache = json.loads(_cache_file().read_text())
    assert cache["_index"] == {"ioggstream": "/api/v1/person/person/1/"}
    assert "ioggstream" not in cache  # the person fetch failed: a retry, not a miss


def test_stale_index_is_not_replaced_by_a_truncated_walk(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, index=[("a", "/api/v1/person/person/1/"), ("b", "/p/2/")])
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    _age_file("_index_fetched_at", 60)

    def two_pages(path: str, timeout: float = 10.0) -> Optional[dict[str, Any]]:  # noqa: ARG001
        if path.endswith("limit=100"):
            return {
                "objects": [{"value": "a", "person": "/api/v1/person/person/1/"}],
                "meta": {"next": "/api/v1/page2"},
            }
        return None

    monkeypatch.setattr(dtg, "_get_json", two_pages)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    cache = json.loads(_cache_file().read_text())
    assert set(cache["_index"]) == {"a", "b"}


def test_expired_hit_is_refreshed_and_a_failed_refresh_keeps_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    emails = {"1": ["old@x.example"]}
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/1/")],
        persons={"/api/v1/person/person/1/": "Roberto Polli"},
        emails=emails,
    )
    dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    _age_file("ioggstream", 40)
    emails["1"] = ["old@x.example", "new@x.example"]
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert out["ioggstream"].emails == ["old@x.example", "new@x.example"]

    _age_file("ioggstream", 40)
    real = dtg._get_json  # pylint: disable=protected-access

    def flaky(path: str, timeout: float = 10.0) -> Optional[dict[str, Any]]:
        if "personextresource" in path:
            return real(path, timeout)
        return None

    monkeypatch.setattr(dtg, "_get_json", flaky)
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert out["ioggstream"].emails == ["old@x.example", "new@x.example"]


def test_refreshes_per_run_are_capped(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = dtg._MAX_REFRESH  # pylint: disable=protected-access
    n = cap + 5
    logins = [f"user{i}" for i in range(n)]
    calls: List[str] = []
    _stub(
        monkeypatch,
        index=[(login, f"/api/v1/person/person/{i}/") for i, login in enumerate(logins)],
        persons={f"/api/v1/person/person/{i}/": f"User {i}" for i in range(n)},
        calls=calls,
    )
    dtg.resolve_via_datatracker(logins, verbose=Verbosity.QUIET)
    for login in logins:
        _age_file(login, 40)
    del calls[:]
    out = dtg.resolve_via_datatracker(logins, verbose=Verbosity.QUIET)
    assert len(out) == n  # the unrefreshed ones still answer from the cache
    assert len([c for c in calls if "/email/" in c]) == cap


def test_entries_missing_from_the_index_do_not_starve_real_refreshes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = dtg._MAX_REFRESH  # pylint: disable=protected-access
    old = "2020-01-01T00:00:00Z"
    gone = [f"gone{i}" for i in range(cap + 10)]
    cache: dict[str, Any] = {
        "_index": {"live": "/api/v1/person/person/1/"},
        "_index_fetched_at": dtg.identity_cache.now_iso(),
        "live": {"name": "Old Name", "emails": ["old@x.example"], "fetched_at": old},
    }
    for login in gone:
        cache[login] = {"name": "G", "emails": ["g@x.example"], "fetched_at": old}
    _cache_file().parent.mkdir(parents=True, exist_ok=True)
    _cache_file().write_text(json.dumps(cache))
    _stub(
        monkeypatch,
        index=[("live", "/api/v1/person/person/1/")],
        persons={"/api/v1/person/person/1/": "New Name"},
        emails={"1": ["new@x.example"]},
    )
    out = dtg.resolve_via_datatracker(["live", *gone], verbose=Verbosity.QUIET)
    assert out["live"].name == "New Name"
    assert out["gone0"].emails == ["g@x.example"]  # kept, not refreshed


def test_empty_refresh_keeps_the_good_entry_and_restamps_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    emails: dict[str, list[str]] = {"1": ["a@x.example"]}
    persons = {"/api/v1/person/person/1/": "Roberto Polli"}
    _stub(
        monkeypatch,
        index=[("ioggstream", "/api/v1/person/person/1/")],
        persons=persons,
        emails=emails,
    )
    dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    _age_file("ioggstream", 40)
    emails["1"] = []
    persons["/api/v1/person/person/1/"] = ""
    out = dtg.resolve_via_datatracker(["ioggstream"], verbose=Verbosity.QUIET)
    assert out["ioggstream"].name == "Roberto Polli"
    entry = json.loads(_cache_file().read_text())["ioggstream"]
    assert entry["emails"] == ["a@x.example"]
    assert not dtg.identity_cache.is_expired(entry["fetched_at"])


def test_empty_walk_does_not_replace_a_healthy_index(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = [(f"u{i}", f"/api/v1/person/person/{i}/") for i in range(10)]
    _stub(monkeypatch, index=index)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    _age_file("_index_fetched_at", 60)
    stale = json.loads(_cache_file().read_text())["_index_fetched_at"]
    del index[:]
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    cache = json.loads(_cache_file().read_text())
    assert len(cache["_index"]) == 10
    assert cache["_index_fetched_at"] == stale  # not restamped as fresh


def test_multi_page_rebuild_replaces_the_stale_index(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, index=[("a", "/api/v1/person/person/1/")])
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    _age_file("_index_fetched_at", 60)

    def pages(path: str, timeout: float = 10.0) -> Optional[dict[str, Any]]:  # noqa: ARG001
        if path.endswith("page2"):
            return {
                "objects": [{"value": "c", "person": "/api/v1/person/person/3/"}],
                "meta": {"next": None},
            }
        return {
            "objects": [
                {"value": "a", "person": "/api/v1/person/person/1/"},
                {"value": "b", "person": "/api/v1/person/person/2/"},
            ],
            "meta": {"next": "/api/v1/page2"},
        }

    monkeypatch.setattr(dtg, "_get_json", pages)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    cache = json.loads(_cache_file().read_text())
    assert set(cache["_index"]) == {"a", "b", "c"}
    assert not dtg.identity_cache.is_expired(cache["_index_fetched_at"])


def test_index_expires_after_seven_days_not_thirty(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    _stub(monkeypatch, index=[("a", "/api/v1/person/person/1/")], calls=calls)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    walks = len([c for c in calls if "personextresource" in c])
    _age_file("_index_fetched_at", 6)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    assert len([c for c in calls if "personextresource" in c]) == walks
    _age_file("_index_fetched_at", 8)
    dtg.resolve_via_datatracker(["zzz"], verbose=Verbosity.QUIET)
    assert len([c for c in calls if "personextresource" in c]) == walks + 1


def test_failed_refresh_backs_off_and_spends_no_slot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = dtg._MAX_REFRESH  # pylint: disable=protected-access
    old = "2020-01-01T00:00:00Z"
    dead = [f"dead{i}" for i in range(cap)]
    live_uri = "/api/v1/person/person/1/"
    cache: dict[str, Any] = {
        "_index": {login: f"/api/v1/person/person/9{i}/" for i, login in enumerate(dead)},
        "_index_fetched_at": dtg.identity_cache.now_iso(),
        "live": {"name": "Old", "emails": ["o@x.example"], "fetched_at": old},
    }
    cache["_index"]["live"] = live_uri
    for login in dead:
        cache[login] = {"name": "D", "emails": ["d@x.example"], "fetched_at": old}
    _cache_file().parent.mkdir(parents=True, exist_ok=True)
    _cache_file().write_text(json.dumps(cache))
    calls: List[str] = []
    # The dead people's records are gone (404 -> None); `live` resolves.
    _stub(
        monkeypatch,
        index=[],
        persons={live_uri: "Live Person"},
        emails={"1": ["n@x.example"]},
        calls=calls,
    )
    logins = [*dead, "live"]
    out = dtg.resolve_via_datatracker(logins, verbose=Verbosity.QUIET)
    # The failures kept their old values and were stamped; live was starved
    # this run by the cap (dead ones came first).
    assert out["dead0"].emails == ["d@x.example"]
    stored = json.loads(_cache_file().read_text())
    assert all("checked_at" in stored[d] for d in dead)
    assert stored["dead0"]["fetched_at"] == old
    assert stored["live"]["fetched_at"] == old
    # Next run: the dead ones are backed off, so they cost no requests and no
    # slot, and the live one is refreshed.
    del calls[:]
    out = dtg.resolve_via_datatracker(logins, verbose=Verbosity.QUIET)
    assert out["live"].name == "Live Person"
    assert len([c for c in calls if "/email/" in c]) == 1
    assert out["dead0"].emails == ["d@x.example"]


def test_back_off_ends_after_the_ttl(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri = "/api/v1/person/person/1/"
    old = "2020-01-01T00:00:00Z"
    cache: dict[str, Any] = {
        "_index": {"a": uri},
        "_index_fetched_at": dtg.identity_cache.now_iso(),
        "a": {
            "name": "Old",
            "emails": [],
            "fetched_at": old,
            "checked_at": (datetime.now(timezone.utc) - timedelta(days=31)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
    }
    _cache_file().parent.mkdir(parents=True, exist_ok=True)
    _cache_file().write_text(json.dumps(cache))
    _stub(monkeypatch, index=[], persons={uri: "New"}, emails={"1": ["n@x.example"]})
    out = dtg.resolve_via_datatracker(["a"], verbose=Verbosity.QUIET)
    assert out["a"].name == "New"
    assert "checked_at" not in json.loads(_cache_file().read_text())["a"]


def test_checked_at_is_harmless_to_older_readers_and_merge() -> None:
    entry = {
        "name": "N",
        "emails": ["e@x.example"],
        "fetched_at": "2026-01-01T00:00:00Z",
        "checked_at": "2026-02-01T00:00:00Z",
    }
    # An older reader reads name/emails only; a newer resolution still wins.
    newer = {"name": "M", "emails": [], "fetched_at": "2026-03-01T00:00:00Z"}
    assert dtg.merge_cache({"a": entry}, {"a": newer})["a"] == newer
    assert dtg.merge_cache({"a": newer}, {"a": entry})["a"] == newer
