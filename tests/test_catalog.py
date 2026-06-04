"""Tests for the effort catalog: reader (`ietf_llm.catalog`) and the
Datatracker mirror writer (`ietf_llm.gather.catalog`).

The reader is exercised against a small seeded `_catalog/catalog.json`;
the writer against a stubbed HTTP session so no HTTP is hit. The final
block is a writer->reader round-trip — drive `ensure_catalog_index` with
canned Datatracker payloads, then read the bytes back through
`render_efforts` — the durable guard against writer/reader format drift
(a derived `catalog.json` makes that drift especially easy to miss).

Source payloads mirror the Datatracker group collection shape:
  {"objects": [{acronym, name, type:<uri>, state:<uri>, parent:<uri>,
                resource_uri:<uri>, description}, ...]}
where type/state are name URIs (`.../wg/`) and parent points at an
area-typed group object carried in the same payload.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm import catalog
from ietf_llm.gather import catalog as gather_catalog

# --- Synthetic Datatracker payloads ---------------------------------------

# Two areas, two WGs, one RG, plus a `team` that must be filtered out.
# httpbis: acronym/name carry the topic; quic: description-only topic
# ("congestion"); modml: an RG whose name carries the topic ("Machine
# Learning"). The team is noise the projector must drop.
_AREA_WIT = {
    "acronym": "wit",
    "name": "Web and Internet Transport",
    "type": "/api/v1/name/grouptypename/area/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/2412/",
    "parent": None,
    "description": "",
}
_AREA_IRTF = {
    "acronym": "irtf",
    "name": "IRTF",
    "type": "/api/v1/name/grouptypename/area/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/9/",
    "parent": None,
    "description": "",
}
_WG_HTTPBIS = {
    "acronym": "httpbis",
    "name": "HTTP",
    "type": "/api/v1/name/grouptypename/wg/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/1718/",
    "parent": "/api/v1/group/group/2412/",
    "description": "Hypertext Transfer Protocol semantics and caching.",
}
_WG_QUIC = {
    "acronym": "quic",
    "name": "QUIC",
    "type": "/api/v1/name/grouptypename/wg/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/2161/",
    "parent": "/api/v1/group/group/2412/",
    "description": "A transport with loss recovery and congestion control.",
}
_RG_MODML = {
    "acronym": "modml",
    "name": "Machine Learning Modelling",
    "type": "/api/v1/name/grouptypename/rg/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/3001/",
    "parent": "/api/v1/group/group/9/",
    "description": "Applying ML to network modelling.",
}
_TEAM_NOISE = {
    "acronym": "tools",
    "name": "Tools Team",
    "type": "/api/v1/name/grouptypename/team/",
    "state": "/api/v1/name/groupstatename/active/",
    "resource_uri": "/api/v1/group/group/5000/",
    "parent": "/api/v1/group/group/2412/",
    "description": "Congestion is not the topic here; this must be dropped.",
}

_ACTIVE_OBJECTS = [
    _AREA_WIT,
    _AREA_IRTF,
    _WG_HTTPBIS,
    _WG_QUIC,
    _RG_MODML,
    _TEAM_NOISE,
]
# One BoF-state group, surfacing via the second source slice.
_BOF_GROUP = {
    "acronym": "newbof",
    "name": "Emerging Topic",
    "type": "/api/v1/name/grouptypename/wg/",
    "state": "/api/v1/name/groupstatename/bof/",
    "resource_uri": "/api/v1/group/group/6000/",
    "parent": "/api/v1/group/group/2412/",
    "description": "A proposed effort on congestion at the edge.",
}

# The projected records the writer should produce from the above.
_EXPECTED_ACRONYMS = {"httpbis", "quic", "modml", "newbof"}


def _seed_catalog(home: Path, efforts: List[Dict[str, Any]]) -> Path:
    """Write a derived catalog.json into the sandbox's `_catalog/` dir."""
    cat_dir = home / ".cache" / "ietf-llm" / catalog.CATALOG_DIR
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / catalog.CATALOG_FILE).write_text(
        json.dumps(efforts), encoding="utf-8"
    )
    return cat_dir


def _effort(
    acronym: str,
    name: str = "",
    description: str = "",
    type_: str = "wg",
    area: str = "wit",
) -> Dict[str, Any]:
    return {
        "acronym": acronym,
        "name": name or acronym.upper(),
        "type": type_,
        "state": "active",
        "area": area,
        "area_name": "Web and Internet Transport",
        "description": description,
    }


def _mark_cached(home: Path, acronym: str) -> None:
    """Create the `<acronym>/files/` dir so `_is_cached` sees it."""
    (home / ".cache" / "ietf-llm" / acronym / "files").mkdir(
        parents=True, exist_ok=True
    )


@pytest.fixture(autouse=True)
def _reset_reader_cache() -> Any:
    """The reader memoises on mtime in a module global; reset it around
    every test so one test's parsed catalog can't leak into another."""
    catalog._CACHE = None
    yield
    catalog._CACHE = None


# --- Reader: ranking -------------------------------------------------------


def test_acronym_exact_match_ranks_first(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [
            _effort("quic", description="nothing relevant here"),
            _effort("other", "QUIC Adjacent", "a quic-ish thing"),
        ],
    )
    out = catalog.render_efforts("quic")
    # Exact acronym beats a name/description mention of the same word.
    first_line = [ln for ln in out.splitlines() if ln.startswith("- ")][0]
    assert "**quic**" in first_line


def test_name_word_match(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [_effort("modml", "Machine Learning Modelling", "network stuff")],
    )
    out = catalog.render_efforts("machine learning")
    assert "**modml**" in out


def test_description_match(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [_effort("quic", "QUIC", "loss recovery and congestion control")],
    )
    out = catalog.render_efforts("congestion")
    assert "**quic**" in out


def test_short_term_only_matches_acronym(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [
            _effort("rg", "Routing", "no match in body"),
            _effort("mail", "Email", "ai appears inside email but is too short"),
        ],
    )
    # "ai" is below PREFIX_LEN: it must not match the "ai" inside "email",
    # but it should still match nothing-vs-acronym cleanly (no crash).
    out = catalog.render_efforts("ai")
    assert "No active efforts match" in out


def test_acronym_short_term_matches(isolated_home: Path) -> None:
    _seed_catalog(isolated_home, [_effort("tls", "Transport Layer Security")])
    out = catalog.render_efforts("tls")
    assert "**tls**" in out


def test_multi_term_scores_accumulate(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [
            _effort("quic", "QUIC", "congestion control transport"),
            _effort("httpbis", "HTTP", "congestion is mentioned once"),
        ],
    )
    out = catalog.render_efforts("quic congestion")
    # quic matches both terms (acronym + description); httpbis only one.
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert "**quic**" in lines[0]


def test_no_match_message(isolated_home: Path) -> None:
    _seed_catalog(isolated_home, [_effort("quic", "QUIC", "transport")])
    assert "No active efforts match" in catalog.render_efforts("zzzplausible")


def test_empty_query_message(isolated_home: Path) -> None:
    _seed_catalog(isolated_home, [_effort("quic")])
    assert "Give a topic" in catalog.render_efforts("   ")


def test_limit_truncates_and_notes_total(isolated_home: Path) -> None:
    _seed_catalog(
        isolated_home,
        [
            _effort("quic", "QUIC", "transport one"),
            _effort("tcpm", "TCPM", "transport two"),
            _effort("tsvwg", "TSVWG", "transport three"),
        ],
    )
    out = catalog.render_efforts("transport", limit=2)
    assert "3 efforts" in out and "showing 2" in out


def test_not_gathered_message(isolated_home: Path) -> None:
    # No seed: the catalog doesn't exist.
    assert "has not been gathered" in catalog.render_efforts("anything")


# --- Reader: cached tag ----------------------------------------------------


def test_cached_effort_tagged(isolated_home: Path) -> None:
    _seed_catalog(isolated_home, [_effort("httpbis", "HTTP", "transport")])
    _mark_cached(isolated_home, "httpbis")
    out = catalog.render_efforts("transport")
    assert "✓ cached" in out


def test_uncached_effort_shows_gather_hint(isolated_home: Path) -> None:
    _seed_catalog(isolated_home, [_effort("httpbis", "HTTP", "transport")])
    out = catalog.render_efforts("transport")
    assert "not gathered" in out
    assert "ietf-llm <acronym>" in out  # the gather hint footer


# --- Writer: ensure_catalog_index -----------------------------------------


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: bytes = b'{"objects": []}',
        etag: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.headers: Dict[str, str] = {}
        if etag is not None:
            self.headers["ETag"] = etag

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests  # pylint: disable=import-outside-toplevel

            raise requests.HTTPError(f"status {self.status_code}")


def _body_for(url: str) -> bytes:
    objects = _ACTIVE_OBJECTS if "state=active" in url else [_BOF_GROUP]
    return json.dumps({"objects": objects}).encode("utf-8")


def _install_stub(monkeypatch: pytest.MonkeyPatch, handler: Any) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_get(
        url: str, headers: Optional[Dict[str, str]] = None, timeout: Any = None
    ):  # noqa: ANN202,ARG001
        calls.append({"url": url, "headers": headers or {}})
        return handler(url, headers or {})

    monkeypatch.setattr(gather_catalog.http_session(), "get", fake_get)
    return calls


def _read_catalog(home: Path) -> List[Dict[str, Any]]:
    path = home / ".cache" / "ietf-llm" / catalog.CATALOG_DIR / catalog.CATALOG_FILE
    return json.loads(path.read_text(encoding="utf-8"))


def test_writer_builds_catalog_filtered_to_groups(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    efforts = _read_catalog(isolated_home)
    acronyms = {e["acronym"] for e in efforts}
    # WGs + RG + BoF kept; the area objects and the team are dropped.
    assert acronyms == _EXPECTED_ACRONYMS
    assert "wit" not in acronyms and "tools" not in acronyms


def test_writer_resolves_area_from_payload(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    efforts = {e["acronym"]: e for e in _read_catalog(isolated_home)}
    assert efforts["httpbis"]["area"] == "wit"
    assert efforts["httpbis"]["area_name"] == "Web and Internet Transport"
    assert efforts["modml"]["area"] == "irtf"


def test_writer_projects_slug_fields(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    efforts = {e["acronym"]: e for e in _read_catalog(isolated_home)}
    assert efforts["httpbis"]["type"] == "wg"
    assert efforts["modml"]["type"] == "rg"
    assert efforts["newbof"]["state"] == "bof"


def test_writer_writes_raw_sources_and_etags(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    cat_dir = isolated_home / ".cache" / "ietf-llm" / catalog.CATALOG_DIR
    files = set(os.listdir(cat_dir))
    assert {"raw-active.json", "raw-bof.json", "catalog.json"} <= files
    assert (cat_dir / "raw-active.json.etag").read_text() == 'W/"v1"'
    assert not any(f.endswith(".tmp") for f in files)  # atomic, no leftovers


def test_writer_first_fetch_sends_no_conditional_header(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    assert calls and all("If-None-Match" not in c["headers"] for c in calls)


def test_writer_revalidates_with_if_none_match(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_catalog.ensure_catalog_index(force=True)
    assert calls and all(
        c["headers"].get("If-None-Match") == 'W/"v1"' for c in calls
    )


def test_writer_304_keeps_catalog_and_touches_mtime(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    cat_path = (
        isolated_home / ".cache" / "ietf-llm" / catalog.CATALOG_DIR / "catalog.json"
    )
    original = cat_path.read_bytes()
    old = cat_path.stat().st_mtime
    os.utime(cat_path, (old - 1000, old - 1000))
    # Both sources 304: catalog unchanged but its mtime is refreshed (TTL
    # restart), and no rebuild happens.
    _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_catalog.ensure_catalog_index(force=True)
    assert cat_path.read_bytes() == original
    assert cat_path.stat().st_mtime > old - 1000


def test_writer_ttl_guard_skips_network(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    # A fresh catalog.json (just written) means a non-forced call must not
    # hit the network at all.
    calls = _install_stub(monkeypatch, lambda url, hdrs: _FakeResponse(304))
    gather_catalog.ensure_catalog_index(force=False)
    assert not calls


def test_writer_never_raises_on_network_error(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import requests  # pylint: disable=import-outside-toplevel

    def boom(url: str, headers: Any) -> Any:  # noqa: ARG001
        raise requests.ConnectionError("down")

    _install_stub(monkeypatch, boom)
    gather_catalog.ensure_catalog_index(force=True)  # must not raise
    cat_path = (
        isolated_home / ".cache" / "ietf-llm" / catalog.CATALOG_DIR / "catalog.json"
    )
    assert not cat_path.exists()  # nothing built, but no crash


# --- Writer -> reader round-trip ------------------------------------------


def test_round_trip_writer_to_reader(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the writer with canned Datatracker payloads, then read the
    bytes back through the reader — the guard against format drift."""
    _install_stub(
        monkeypatch,
        lambda url, hdrs: _FakeResponse(200, _body_for(url), etag='W/"v1"'),
    )
    gather_catalog.ensure_catalog_index(force=True)
    catalog._CACHE = None  # force a fresh load of the just-written file
    out = catalog.render_efforts("congestion")
    # quic ("congestion control" in its description) and newbof
    # ("congestion at the edge") both surface from the writer's output.
    assert "**quic**" in out
    assert "**newbof**" in out
    # And the area resolved end-to-end into the rendered facets.
    assert "wit" in out
