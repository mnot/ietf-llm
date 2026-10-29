"""Tests for the Datatracker mail-address -> person-id resolver
(ietf_llm.gather.sources.datatracker_people).

Covers the contract:
- resolve a batch of addresses to their Datatracker person uris in one
  (or a few, chunked) `address__in` request
- addresses Datatracker doesn't recognise are cached as confirmed misses
- hits and misses are cached, so a second pass makes no further requests
- a transient failure (None from _get_json) is not cached
- addresses are URL-encoded into the query
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import ietf_llm.gather.sources.datatracker_people as dtp
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_cache_dir


def _cache_file() -> Path:
    return Path(get_cache_dir()) / "_datatracker-people.json"


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mapping: Dict[str, str],
    calls: Optional[List[str]] = None,
) -> None:
    """Install a fake `_get_json` resolving addresses from `mapping`
    (address -> person_uri). The fake reads the `address__in` query, decodes
    each address, and returns a row only for addresses present in `mapping`."""

    def fake(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:  # noqa: ARG001
        if calls is not None:
            calls.append(path_or_url)
        from urllib.parse import unquote  # pylint: disable=import-outside-toplevel

        joined = path_or_url.split("address__in=", 1)[1].split("&", 1)[0]
        objects = []
        for raw in joined.split(","):
            addr = unquote(raw).lower()
            if addr in mapping:
                objects.append({"address": addr, "person": mapping[addr]})
        return {"objects": objects}

    monkeypatch.setattr(dtp, "_get_json", fake)


def test_resolves_addresses_to_person_uris(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        mapping={
            "mnot@mnot.net": "/api/v1/person/person/1/",
            "mnot@fastly.com": "/api/v1/person/person/1/",
        },
    )
    out = dtp.resolve_addresses(
        ["mnot@mnot.net", "mnot@fastly.com"], verbose=Verbosity.QUIET
    )
    assert out == {
        "mnot@mnot.net": "/api/v1/person/person/1/",
        "mnot@fastly.com": "/api/v1/person/person/1/",
    }


def test_unmatched_address_is_cached_miss(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, mapping={"known@x.example": "/api/v1/person/person/2/"})
    out = dtp.resolve_addresses(
        ["unknown@y.example"], verbose=Verbosity.QUIET
    )
    assert out == {}
    cache = json.loads(_cache_file().read_text())
    assert cache["unknown@y.example"]["person"] is None
    assert cache["unknown@y.example"]["fetched_at"]


def test_second_pass_makes_no_requests(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    _stub(
        monkeypatch,
        mapping={"mnot@mnot.net": "/api/v1/person/person/1/"},
        calls=calls,
    )
    dtp.resolve_addresses(
        ["mnot@mnot.net", "unknown@y.example"], verbose=Verbosity.QUIET
    )
    first = len(calls)
    assert first > 0
    # Both the hit and the miss are now cached on disk.
    dtp.resolve_addresses(
        ["mnot@mnot.net", "unknown@y.example"], verbose=Verbosity.QUIET
    )
    assert len(calls) == first


def _age(entry_key: str, days: int) -> None:
    """Backdate one cache entry's stamp by `days`."""
    data = json.loads(_cache_file().read_text())
    old = datetime.now(timezone.utc) - timedelta(days=days)
    data[entry_key] = {
        **(data[entry_key] or {}),
        "fetched_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _cache_file().write_text(json.dumps(data))


def test_expired_miss_is_requeried_and_can_resolve(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping: Dict[str, str] = {}
    calls: List[str] = []
    _stub(monkeypatch, mapping=mapping, calls=calls)
    assert dtp.resolve_addresses(["new@x.example"], verbose=Verbosity.QUIET) == {}
    # The person registers the address later.
    mapping["new@x.example"] = "/api/v1/person/person/7/"
    # Within the TTL the recorded miss stands.
    _age("new@x.example", 5)
    assert dtp.resolve_addresses(["new@x.example"], verbose=Verbosity.QUIET) == {}
    assert len(calls) == 1
    # Past it, the address is asked about again.
    _age("new@x.example", 45)
    out = dtp.resolve_addresses(["new@x.example"], verbose=Verbosity.QUIET)
    assert out == {"new@x.example": "/api/v1/person/person/7/"}
    assert len(calls) == 2
    assert json.loads(_cache_file().read_text())["new@x.example"]["person"]


def test_legacy_bare_null_miss_is_requeried_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Misses written before they carried a stamp have no age to trust.
    _cache_file().parent.mkdir(parents=True, exist_ok=True)
    _cache_file().write_text(json.dumps({"old@x.example": None}))
    calls: List[str] = []
    _stub(monkeypatch, mapping={}, calls=calls)
    dtp.resolve_addresses(["old@x.example"], verbose=Verbosity.QUIET)
    assert len(calls) == 1
    assert json.loads(_cache_file().read_text())["old@x.example"]["fetched_at"]
    dtp.resolve_addresses(["old@x.example"], verbose=Verbosity.QUIET)
    assert len(calls) == 1


def test_old_hit_is_kept_without_requery(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    _stub(
        monkeypatch, mapping={"a@x.example": "/api/v1/person/person/1/"}, calls=calls
    )
    dtp.resolve_addresses(["a@x.example"], verbose=Verbosity.QUIET)
    _age("a@x.example", 400)
    out = dtp.resolve_addresses(["a@x.example"], verbose=Verbosity.QUIET)
    assert out == {"a@x.example": "/api/v1/person/person/1/"}
    assert len(calls) == 1


def test_stamped_miss_does_not_displace_newer_hit_on_merge() -> None:
    hit = {"person": "/api/v1/person/person/1/", "fetched_at": "2026-02-01T00:00:00Z"}
    miss = {"person": None, "fetched_at": "2026-01-01T00:00:00Z"}
    merged = dtp.merge_cache({"a": miss, "b": None}, {"a": hit, "b": miss})
    assert merged["a"] == hit
    assert merged["b"] == miss


def test_transient_failure_not_cached(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dtp, "_get_json", lambda path, timeout=10.0: None)
    out = dtp.resolve_addresses(["mnot@mnot.net"], verbose=Verbosity.QUIET)
    assert out == {}
    # Nothing cached → next run retries.
    assert not _cache_file().exists()


def test_addresses_are_url_encoded(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    _stub(
        monkeypatch,
        mapping={"mnot+ietf@mnot.net": "/api/v1/person/person/1/"},
        calls=calls,
    )
    out = dtp.resolve_addresses(["mnot+ietf@mnot.net"], verbose=Verbosity.QUIET)
    # The '+' and '@' must be percent-encoded in the query, not sent raw.
    assert "mnot%2Bietf%40mnot.net" in calls[0]
    assert out == {"mnot+ietf@mnot.net": "/api/v1/person/person/1/"}


def test_chunks_large_address_lists(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    addresses = [f"user{i}@x.example" for i in range(120)]
    _stub(
        monkeypatch,
        mapping={a: f"/api/v1/person/person/{i}/" for i, a in enumerate(addresses)},
        calls=calls,
    )
    out = dtp.resolve_addresses(addresses, verbose=Verbosity.QUIET)
    assert len(out) == 120
    # 120 addresses at 50/chunk → 3 requests.
    assert len(calls) == 3


def test_hit_beats_a_newer_miss_on_merge_in_either_order() -> None:
    hit = {"person": "/api/v1/person/person/1/", "fetched_at": "2026-02-01T00:00:00Z"}
    miss = {"person": None, "fetched_at": "2026-03-01T00:00:00Z"}
    assert dtp.merge_cache({"a": hit}, {"a": miss})["a"] == hit
    assert dtp.merge_cache({"a": miss}, {"a": hit})["a"] == hit
    newer = {
        "person": "/api/v1/person/person/2/",
        "fetched_at": "2026-04-01T00:00:00Z",
    }
    assert dtp.merge_cache({"a": hit}, {"a": newer})["a"] == newer
