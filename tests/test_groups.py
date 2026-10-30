"""Tests for the chartered-group mirror: the writer
(`gather.sources.groups`), the reader (`singletons.groups`), the charter
chunker, the MCP tools, and the charter-aware `find_efforts`.

The writer is driven with canned Datatracker payloads through its `fetch`
seam, then the bytes it wrote are read back through the reader tools: the
writer->reader round trip is what catches format drift.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from ietf_llm import embeddings
from ietf_llm.embeddings import chunking
from ietf_llm.gather.sources import groups as writer
from ietf_llm.gather.sources.charter import approved_rev
from ietf_llm.gather.sources.groups import _fetch_charter as real_fetch_charter
from ietf_llm.log import Verbosity
from ietf_llm.mcp import groups as tools
from ietf_llm.paths import get_cache_dir
from ietf_llm.singletons import catalog
from ietf_llm.singletons import groups as reader

# --- canned Datatracker payloads --------------------------------------------

_WG = "/api/v1/name/grouptypename/wg/"
_RG = "/api/v1/name/grouptypename/rg/"
_ACTIVE = "/api/v1/name/groupstatename/active/"
_CONCLUDED = "/api/v1/name/groupstatename/conclude/"

_GROUPS = [
    {"acronym": "httpbis", "name": "HTTP", "type": _WG, "state": _ACTIVE,
     "parent": "/api/v1/group/group/100/", "resource_uri": "/api/v1/group/group/1/",
     "description": "HTTP semantics, caching, and the extensions around them, maintained as a family.",
     "list_email": "ietf-http-wg@w3.org"},
    {"acronym": "l3vpn", "name": "Layer 3 VPNs", "type": _WG, "state": _CONCLUDED,
     "parent": "/api/v1/group/group/101/", "resource_uri": "/api/v1/group/group/2/",
     "description": "BGP/MPLS IP VPN solutions for provider-provisioned layer-3 virtual private networks."},
    {"acronym": "iccrg", "name": "Internet Congestion Control", "type": _RG, "state": _ACTIVE,
     "parent": "/api/v1/group/group/102/", "resource_uri": "/api/v1/group/group/3/",
     "description": "Research into congestion control for the Internet and its evolving transports."},
]
_AREAS = [
    {"acronym": "wit", "name": "Web and Internet Transport", "resource_uri": "/api/v1/group/group/100/"},
    {"acronym": "rtg", "name": "Routing", "resource_uri": "/api/v1/group/group/101/"},
    {"acronym": "irtf", "name": "IRTF", "resource_uri": "/api/v1/group/group/102/"},
]
_CHARTER_DOCS = [
    {"name": "charter-ietf-httpbis", "rev": "09", "group": "/api/v1/group/group/1/"},
    {"name": "charter-ietf-l3vpn", "rev": "05", "group": "/api/v1/group/group/2/"},
    {"name": "charter-irtf-iccrg", "rev": "03", "group": "/api/v1/group/group/3/"},
]
_CHARTERS = {
    "charter-ietf-httpbis": "The HTTP Working Group maintains HTTP semantics and caching.",
    "charter-ietf-l3vpn": "The L3VPN Working Group specifies BGP/MPLS VPNs.",
    "charter-irtf-iccrg": "ICCRG studies congestion control.",
}


def _milestone(mid: int, group: int, desc: str, due: str, state: str = "active",
               resolved: str = "", time_: str = "2024-01-01T00:00:00Z") -> Dict[str, Any]:
    return {"id": mid, "group": f"/api/v1/group/group/{group}/", "desc": desc, "due": due,
            "resolved": resolved, "state": f"/api/v1/name/groupmilestonestatename/{state}/",
            "docs": ["/api/v1/doc/document/draft-ietf-httpbis-cache/"] if mid == 10 else [],
            "time": time_}


class _Datatracker:
    """Canned responses keyed on URL fragments; records every URL asked for."""

    def __init__(self) -> None:
        self.urls: List[str] = []
        self.extra_milestones: List[Dict[str, Any]] = []
        self.fail = False
        self.empty_groups = False

    def __call__(self, url: str, verbosity: Verbosity) -> Optional[List[Dict[str, Any]]]:
        self.urls.append(url)
        if self.fail:
            return None
        if "type__in=area" in url:
            return list(_AREAS)
        if "type__in=wg,rg" in url and "/group/group/" in url:
            return [] if self.empty_groups else list(_GROUPS)
        if "doc/document/?type=charter" in url:
            return list(_CHARTER_DOCS)
        if "groupevent" in url and "info_changed" in url:
            return [{"id": 20, "group": "/api/v1/group/group/1/", "time": "2016-07-27T09:20:49Z",
                     "desc": "Chairs changed to <b>Mark Nottingham, Patrick McManus</b> from Mark Nottingham"},
                    {"id": 21, "group": "/api/v1/group/group/1/", "time": "2019-11-04T06:45:42Z",
                     "desc": "Chairs changed to <b>Mark Nottingham</b> from Mark Nottingham, Patrick McManus"}]
        if "groupevent" in url:
            return [{"id": 1, "group": "/api/v1/group/group/2/", "time": "2014-10-22T22:21:24Z",
                     "desc": "State changed to <b>Concluded</b> from Active"}]
        if "type=new_revision" in url:
            return [{"id": 2, "doc": "/api/v1/doc/document/charter-ietf-httpbis/", "rev": "09",
                     "time": "2025-10-13T21:49:21Z"}]
        if "type=iesg_approved" in url:
            return [{"id": 3, "doc": "/api/v1/doc/document/charter-ietf-httpbis/", "rev": "09",
                     "desc": "IESG has approved the charter", "time": "2025-10-13T21:49:22Z"}]
        if "groupmilestonehistory" in url:
            first = _milestone(10, 1, "Submit cache", "2020-06-30", time_="2020-01-01T00:00:00Z")
            return [dict(first, id=900, milestone="/api/v1/group/groupmilestone/10/")]
        if "groupmilestone" in url:
            rows = [
                _milestone(10, 1, "Submit cache", "2099-01-01", time_="2024-04-26T00:00:00Z"),
                _milestone(11, 1, "Submit semantics", "2022-06-30", resolved="Done",
                           time_="2022-07-01T00:00:00Z"),
                _milestone(12, 3, "Dropped thing", "2023-01-01", state="deleted"),
            ]
            return rows + self.extra_milestones
        if "published_rfc" in url:
            return [{"id": 4, "doc": "/api/v1/doc/document/rfc9110/", "time": "2022-06-06T00:00:00Z"}]
        return []

    def feed_urls(self, fragment: str) -> List[str]:
        return [u for u in self.urls if fragment in u]


def _charter_fetch(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
    return _CHARTERS.get(doc)


def _seed_rfc_mirror(home: Path) -> None:
    rfc = home / ".cache" / "ietf-llm" / "_rfc"
    rfc.mkdir(parents=True)
    (rfc / "rfcs.json").write_text(json.dumps({"RFC9110": {"wg": "httpbis", "title": "HTTP Semantics"}}))
    (rfc / "refs.json").write_text("{}")
    (rfc / "tags.json").write_text("{}")


def _refresh(dt: _Datatracker, **kw: Any) -> None:
    kw.setdefault("embed", False)
    kw.setdefault("force", True)
    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=_charter_fetch, **kw)


@pytest.fixture(name="mirror")
def _mirror(isolated_home: Path) -> _Datatracker:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    _refresh(dt)
    return dt


# --- writer -> reader --------------------------------------------------------


def test_group_round_trip(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_group("httpbis")
    assert "# httpbis — HTTP" in out
    assert "Working Group · active" in out
    assert "Web and Internet Transport (wit)" in out
    assert "rev 09" in out
    assert "maintains HTTP semantics and caching" in out
    assert "Submit cache (draft-ietf-httpbis-cache)" in out
    assert "1 open of 2" in out


def test_concluded_group_is_labelled(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_group("l3vpn")
    assert "concluded" in out
    assert "no longer active" in out
    assert "last state change 2014-10-22" in out


def test_research_group_has_irtf_area(mirror: _Datatracker) -> None:  # noqa: ARG001
    group = reader.get_group("iccrg")
    assert group is not None
    assert group["type"] == "rg" and group["area"] == "irtf"


def test_unknown_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "No chartered group `nope`" in reader.render_group("nope")


def test_timeline_covers_each_source(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_timeline("httpbis")
    assert "Charter revision 09" in out
    assert "IESG has approved the charter (rev 09)" in out
    assert "RFC 9110 published: HTTP Semantics" in out
    assert "Milestone added: Submit cache (due 2020-06)" in out
    assert "Milestone due 2099-01 (was 2020-06): Submit cache" in out
    assert "Milestone done" not in out  # only one version of that milestone
    assert "Chair appointed: Patrick McManus" in out
    assert "Chair stepped down: Patrick McManus" in out
    assert "Chair appointed: Mark Nottingham" not in out  # already a chair
    assert reader.render_timeline("l3vpn").count("State changed to Concluded from Active") == 1


def test_chair_feed_asks_only_for_wg_and_rg_chair_changes(mirror: _Datatracker) -> None:
    (url,) = mirror.feed_urls("desc__startswith=Chair")
    assert "type=info_changed" in url and "group__type__in=wg,rg" in url


def test_chair_feed_backfills_a_mirror_that_predates_it(
    mirror: _Datatracker, isolated_home: Path
) -> None:
    # Upgrade path: state from before the feed existed has watermarks for the
    # other feeds but nothing for chair events.
    (marks_path,) = isolated_home.rglob("watermarks.json")
    marks = json.loads(marks_path.read_text())
    marks.pop("chair-events")
    marks_path.write_text(json.dumps(marks))
    (marks_path.parent / "chair-events.json").unlink()
    (timeline_path,) = isolated_home.rglob(reader.TIMELINE_FILE)
    timeline = json.loads(timeline_path.read_text())
    for events in timeline.values():
        events[:] = [e for e in events if not e["kind"].startswith("chair")]
    timeline_path.write_text(json.dumps(timeline))

    mirror.urls.clear()
    _refresh(mirror)
    (chair_url,) = mirror.feed_urls("desc__startswith=Chair")
    (state_url,) = mirror.feed_urls("type=changed_state")
    assert "time__gte" not in chair_url  # full fetch
    assert "time__gte" in state_url  # the others stay incremental
    assert "Chair appointed: Patrick McManus" in reader.render_timeline("httpbis")


def test_chair_events_for_an_unknown_group_are_dropped() -> None:
    feeds: Dict[str, Dict[str, Dict[str, Any]]] = {
        name: {} for name in writer._FEEDS  # pylint: disable=protected-access
    }
    feeds["chair-events"] = {
        "1": {"id": 1, "group": "/api/v1/group/group/999/",
              "time": "2020-01-01T00:00:00Z",
              "desc": "Chairs changed to <b>A B</b> from "},
    }
    assert writer.build_timeline(feeds, {}, lambda _n: None) == {}


def test_timeline_year_filter(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_timeline("httpbis", 2022)
    assert "RFC 9110" in out and "Charter revision" not in out
    assert "No timeline events" in reader.render_timeline("httpbis", 1999)


def test_dropped_milestones_never_listed(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Dropped thing" not in reader.render_milestones(status="all", include_closed_groups=True)


# --- milestone listing -------------------------------------------------------


def test_milestones_default_to_open(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = reader.render_milestones()
    assert "Submit cache" in out and "Submit semantics" not in out


def test_milestones_done_and_all(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Submit semantics" in reader.render_milestones(status="done")
    both = reader.render_milestones(status="all")
    assert "Submit cache" in both and "Submit semantics" in both


def test_milestones_overdue_and_date_window(mirror: _Datatracker) -> None:  # noqa: ARG001
    mirror.extra_milestones = [_milestone(13, 1, "Late thing", "2020-01-01")]
    _refresh(mirror)
    assert "Late thing" in reader.render_milestones(status="overdue")
    assert "Submit cache" not in reader.render_milestones(status="overdue")
    assert "Late thing" not in reader.render_milestones(due_after="2021-01-01")


def test_milestones_filter_by_area_and_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "Submit cache" in reader.render_milestones(area="wit")
    assert reader.render_milestones(area="rtg") == "No milestones match."
    assert "Submit cache" in reader.render_milestones(group="httpbis")
    assert "No chartered group" in reader.render_milestones(group="nope")


def test_milestones_reject_bad_input(mirror: _Datatracker) -> None:  # noqa: ARG001
    assert "must be one of" in reader.render_milestones(status="soon")
    assert "ISO date" in reader.render_milestones(due_before="next week")


# --- incremental refresh -----------------------------------------------------


def test_second_refresh_asks_only_for_what_changed(mirror: _Datatracker) -> None:
    mirror.urls.clear()
    _refresh(mirror)
    feeds = mirror.feed_urls("groupmilestone/?")
    assert feeds and all("time__gte=2024-04-26T00:00:00" in u for u in feeds)
    history = mirror.feed_urls("groupmilestonehistory")
    assert history and all("time__gte=2020-01-01T00:00:00" in u for u in history)


def test_new_rows_merge_into_stored_ones(mirror: _Datatracker) -> None:
    mirror.extra_milestones = [_milestone(14, 1, "Fresh", "2099-02-01", time_="2026-01-01T00:00:00Z")]
    _refresh(mirror)
    out = reader.render_milestones(group="httpbis", status="all")
    assert "Fresh" in out and "Submit semantics" in out


def test_failed_refresh_keeps_the_mirror(mirror: _Datatracker) -> None:
    before = reader.render_group("httpbis")
    mirror.fail = True
    _refresh(mirror)
    assert reader.render_group("httpbis") == before


def test_ttl_skips_the_network(mirror: _Datatracker) -> None:
    mirror.urls.clear()
    _refresh(mirror, force=False)
    assert mirror.urls == []


# --- charter fetching --------------------------------------------------------


def test_charters_trickle_in_active_first(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    _refresh(dt, batch=1)
    held = [a for a in ("httpbis", "iccrg", "l3vpn") if reader.charter_text(a)]
    assert held == ["httpbis"]  # active before concluded, alphabetical within
    _refresh(dt, batch=1)
    assert reader.charter_text("iccrg") and not reader.charter_text("l3vpn")
    _refresh(dt, batch=5)
    assert reader.charter_text("l3vpn")


def test_group_without_charter_text_falls_back_to_description(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    writer.ensure_groups_index(
        Verbosity.QUIET, fetch=dt, charter_fetch=lambda d, r: None, force=True, embed=False
    )
    assert reader.charter_text("httpbis") is None
    assert "No charter text available" in reader.render_group("httpbis")
    assert "## Description" in Path(reader.charter_file("httpbis")).read_text()


def test_absent_charter_is_not_asked_for_again_at_once(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    calls: List[str] = []

    def absent(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        calls.append(doc)
        return ""

    for _ in range(2):
        writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=absent, force=True, embed=False)
    assert len(calls) == 3


def test_transient_failure_is_retried_on_the_next_batch(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    calls: List[str] = []

    def down(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        calls.append(doc)
        return None

    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=down, force=True, embed=False)
    assert len(calls) == 3 and not reader.charter_text("httpbis")
    writer.ensure_groups_index(Verbosity.QUIET, fetch=dt, charter_fetch=_charter_fetch, force=True, embed=False)
    assert reader.charter_text("httpbis") and reader.charter_text("l3vpn")


def _fake_groups(n: int) -> List[Dict[str, Any]]:
    return [{"acronym": f"g{i:02d}", "state": "active", "charter_doc": f"charter-ietf-g{i:02d}",
             "charter_rev": "01"} for i in range(n)]


def test_batch_stops_after_consecutive_transient_failures() -> None:
    calls: List[str] = []

    def down(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        calls.append(doc)
        return None

    fetched: Dict[str, Dict[str, Any]] = {}
    texts = writer._fetch_charter_batch(_fake_groups(12), fetched, down, 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert len(calls) == 5 and texts == {}
    assert all(v["tries"] == 1 and not v["ok"] for v in fetched.values()) and len(fetched) == 5


def test_a_success_resets_the_failure_streak() -> None:
    answers = iter([None] * 4 + ["text"] + [None] * 4 + ["text"])

    def flaky(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        return next(answers)

    fetched: Dict[str, Dict[str, Any]] = {}
    texts = writer._fetch_charter_batch(_fake_groups(10), fetched, flaky, 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert len(texts) == 2 and sum(v["ok"] for v in fetched.values()) == 2
    assert len(fetched) == 10  # the failures are counted, not forgotten


def test_absent_revisions_do_not_end_a_batch() -> None:
    fetched: Dict[str, Dict[str, Any]] = {}
    texts = writer._fetch_charter_batch(_fake_groups(8), fetched, lambda d, r: "", 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert texts == {} and len(fetched) == 8 and not any(v["ok"] for v in fetched.values())


class _Response:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()

    @property
    def ok(self) -> bool:
        return self.status_code < 400


def _fetch_with(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> Optional[str]:
    def fake_get(url: str, **_kw: Any) -> Any:  # noqa: ARG001
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(writer, "governed_get", fake_get)
    return real_fetch_charter("charter-ietf-x", "01")


def test_charter_fetch_distinguishes_absent_from_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests  # pylint: disable=import-outside-toplevel

    assert _fetch_with(monkeypatch, _Response(200, "the charter")) == "the charter"
    assert _fetch_with(monkeypatch, _Response(404, "<html>")) == ""
    assert _fetch_with(monkeypatch, _Response(410, "gone")) == ""
    assert _fetch_with(monkeypatch, _Response(403, "blocked")) is None
    assert _fetch_with(monkeypatch, _Response(503, "busy")) is None
    assert _fetch_with(monkeypatch, _Response(429, "slow down")) is None
    assert _fetch_with(monkeypatch, _Response(200, "")) is None
    assert _fetch_with(monkeypatch, requests.ConnectionError("down")) is None


def test_new_charter_revision_is_refetched(mirror: _Datatracker) -> None:
    _CHARTER_DOCS[0]["rev"] = "10"
    try:
        _CHARTERS["charter-ietf-httpbis"] = "Revised charter text."
        _refresh(mirror)
        assert "Revised charter text." in reader.render_group("httpbis")
    finally:
        _CHARTER_DOCS[0]["rev"] = "09"
        _CHARTERS["charter-ietf-httpbis"] = "The HTTP Working Group maintains HTTP semantics and caching."


def test_milestone_change_rerenders_without_refetching(mirror: _Datatracker) -> None:
    fetched: List[str] = []

    def spy(doc: str, rev: str) -> Optional[str]:
        fetched.append(doc)
        return _charter_fetch(doc, rev)

    mirror.extra_milestones = [_milestone(15, 1, "Brand new", "2099-03-01", time_="2026-02-01T00:00:00Z")]
    writer.ensure_groups_index(Verbosity.QUIET, fetch=mirror, charter_fetch=spy, force=True, embed=False)
    assert fetched == []
    assert "Brand new" in Path(reader.charter_file("httpbis")).read_text()


def test_unmirrored_cache_reports_not_gathered(isolated_home: Path) -> None:  # noqa: ARG001
    assert reader.render_group("httpbis") == reader.NOT_GATHERED
    assert reader.render_milestones() == reader.NOT_GATHERED
    assert reader.render_timeline("httpbis") == reader.NOT_GATHERED


# --- chunker -----------------------------------------------------------------


def test_charter_chunks_carry_state_area_and_type(mirror: _Datatracker, isolated_home: Path) -> None:  # noqa: ARG001
    path = reader.charter_file("l3vpn")
    chunks = chunking._chunk_file(path, "charters/l3vpn.md")  # pylint: disable=protected-access
    assert chunks
    first = chunks[0]
    assert first.state == "conclude"
    assert first.labels == "area:rtg,type:wg"
    assert first.title == "l3vpn — Layer 3 VPNs"
    assert first.url == "https://datatracker.ietf.org/doc/charter-ietf-l3vpn/"


def test_state_dir_is_not_embedded(mirror: _Datatracker) -> None:  # noqa: ARG001
    files = chunking._eligible_files(reader.groups_files_dir(), "_groups")  # pylint: disable=protected-access
    assert files and all("/state/" not in f for f in files)
    assert all(f.endswith(".md") for f in files)


# --- semantic search ---------------------------------------------------------


class _AreaModel:
    """Scores everything alike, so scoping can only come from the filters."""

    def embed(self, _text: str) -> Iterable[float]:
        return [1.0] + [0.0] * 7

    def embed_multi(self, texts: List[str]) -> Iterable[List[float]]:
        return [list(self.embed(t)) for t in texts]


@pytest.fixture(name="indexed")
def _indexed(mirror: _Datatracker) -> _Datatracker:
    embeddings._MODEL_CACHE["stub"] = _AreaModel()  # pylint: disable=protected-access
    _refresh(mirror, embed=True, embed_model="stub", force=False)
    return mirror


def test_index_is_built_and_hidden_from_corpus_listing(indexed: _Datatracker) -> None:  # noqa: ARG001
    from ietf_llm.paths import cached_wg_names  # pylint: disable=import-outside-toplevel

    assert tools.index_installed()
    assert "_groups" not in cached_wg_names()


def test_semantic_ranking_scopes_by_area(indexed: _Datatracker) -> None:  # noqa: ARG001
    assert set(tools.semantic_ranking("anything")) == {"httpbis", "iccrg", "l3vpn"}
    assert set(tools.semantic_ranking("anything", area="wit")) == {"httpbis"}
    assert set(tools.semantic_ranking("anything", area="Web and Internet Transport")) == {"httpbis"}
    assert set(tools.semantic_ranking("anything", kind="rg")) == {"iccrg"}
    assert set(tools.semantic_ranking("anything", state="concluded")) == {"l3vpn"}


def test_search_groups_filters_and_labels(indexed: _Datatracker) -> None:  # noqa: ARG001
    out = tools.tool_search_groups("anything", state="closed")
    assert "l3vpn" in out and "httpbis" not in out
    assert "concluded" in out
    assert "No chartered groups match" in tools.tool_search_groups("anything", area="sec")


def test_search_groups_falls_back_to_keywords_without_an_index(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = tools.tool_search_groups("congestion control")
    assert "iccrg" in out
    assert "charter index is not installed" in out


def test_search_groups_before_gather(isolated_home: Path) -> None:  # noqa: ARG001
    assert tools.tool_search_groups("x") == reader.NOT_GATHERED


def test_search_below_the_floor_is_not_a_match(indexed: _Datatracker, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setattr(tools, "SEMANTIC_FLOOR", 1.5)
    assert tools.semantic_ranking("anything") == {}


# --- find_efforts ------------------------------------------------------------


def test_find_efforts_includes_concluded_groups_below_active(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("VPN virtual private networks", 10)
    assert "l3vpn" in out and "concluded" in out


def test_find_efforts_prefers_active_on_equal_match(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("congestion", 10, semantic=["l3vpn", "iccrg"])
    assert out.index("**iccrg**") < out.index("**l3vpn**")


def test_find_efforts_admits_semantic_only_matches(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("zzzz unmatched words", 10, semantic=["httpbis"])
    assert "httpbis" in out
    assert "No efforts match" in catalog.render_efforts("zzzz unmatched words", 10)


def test_find_efforts_pins_a_named_group(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("httpbis", 10, semantic=["iccrg", "l3vpn"])
    assert out.index("**httpbis**") < out.index("**iccrg**")


def test_find_efforts_word_equal_to_an_acronym_is_not_pinned(mirror: _Datatracker) -> None:  # noqa: ARG001
    out = catalog.render_efforts("httpbis congestion", 10, semantic=["iccrg"])
    assert out.index("**iccrg**") < out.index("**httpbis**")


def test_find_efforts_falls_back_to_the_catalog(isolated_home: Path) -> None:
    cat = Path(get_cache_dir()) / "_catalog"
    cat.mkdir(parents=True)
    (cat / "catalog.json").write_text(json.dumps([
        {"acronym": "quic", "name": "QUIC", "type": "wg", "state": "active",
         "area": "wit", "area_name": "", "description": "Transport."}
    ]))
    assert "**quic**" in catalog.render_efforts("quic", 5)


def test_row_summary_replaces_a_stub_description(mirror: _Datatracker) -> None:  # noqa: ARG001
    group = dict(reader.get_group("httpbis") or {}, description="Background")
    assert "maintains HTTP semantics" in reader.summary(group)


def test_mirror_is_stamped_fresh(mirror: _Datatracker) -> None:  # noqa: ARG001
    path = Path(reader.groups_files_dir()) / reader.GROUPS_FILE
    assert time.time() - path.stat().st_mtime < 60


# --- seed store --------------------------------------------------------------


def _publish(tmp_path: Path) -> str:
    from ietf_llm.seed import publish  # pylint: disable=import-outside-toplevel

    base = str(tmp_path / "store")
    publish.publish_store(
        publish.generation_dir(base),
        add=[reader.GROUPS_CORPUS],
        no_gather=True,
        gather=lambda c, m: None,
    )
    return base


def test_published_bundle_carries_text_state_and_index(indexed: _Datatracker, tmp_path: Path) -> None:  # noqa: ARG001
    import tarfile  # pylint: disable=import-outside-toplevel

    from ietf_llm.seed import publish  # pylint: disable=import-outside-toplevel

    base = _publish(tmp_path)
    bundles = list(Path(publish.generation_dir(base)).glob("_groups/*.tar.gz"))
    assert len(bundles) == 1
    with tarfile.open(bundles[0]) as tar:
        names = set(tar.getnames())
    assert {"files/groups.json", "files/charters/httpbis.md", "files/state/watermarks.json",
            "embeddings.db"} <= names


def test_fresh_machine_installs_the_seed_then_only_asks_for_the_delta(
    indexed: _Datatracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil  # pylint: disable=import-outside-toplevel

    base = _publish(tmp_path)
    shutil.rmtree(Path(get_cache_dir()) / reader.GROUPS_CORPUS)
    assert reader.load_groups() is None
    monkeypatch.setenv("IETF_LLM_SEED_URL", base)

    fresh = _Datatracker()
    writer.ensure_groups_index(
        Verbosity.QUIET, fetch=fresh, charter_fetch=_charter_fetch, embed=False, force=True
    )
    assert reader.get_group("httpbis") is not None
    assert reader.charter_text("httpbis")
    milestone_urls = fresh.feed_urls("groupmilestone/?")
    assert milestone_urls and all("time__gte=" in u for u in milestone_urls)


def test_no_seed_means_a_cold_build(
    indexed: _Datatracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil  # pylint: disable=import-outside-toplevel

    base = _publish(tmp_path)
    shutil.rmtree(Path(get_cache_dir()) / reader.GROUPS_CORPUS)
    monkeypatch.setenv("IETF_LLM_SEED_URL", base)
    fresh = _Datatracker()
    writer.ensure_groups_index(
        Verbosity.QUIET, fetch=fresh, charter_fetch=_charter_fetch, embed=False, force=True,
        seed=False,
    )
    assert all("time__gte=" not in u for u in fresh.feed_urls("groupmilestone/?"))


def test_an_existing_mirror_is_never_replaced_by_the_seed(
    indexed: _Datatracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _publish(tmp_path)
    monkeypatch.setenv("IETF_LLM_SEED_URL", base)
    called: List[bool] = []
    monkeypatch.setattr(writer, "install_seed", lambda v: called.append(True) or True)
    _refresh(indexed, force=False)
    assert called == []


def test_seed_members_are_not_offered_as_corpora_to_gather(
    indexed: _Datatracker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: ARG001
) -> None:
    from ietf_llm.mcp import corpus as corpus_tools  # pylint: disable=import-outside-toplevel
    from ietf_llm.seed import catalog as seed_catalog  # pylint: disable=import-outside-toplevel
    from ietf_llm.seed import fetch as seed_fetch  # pylint: disable=import-outside-toplevel
    from ietf_llm.seed import generation  # pylint: disable=import-outside-toplevel

    base = _publish(tmp_path)
    monkeypatch.setenv("IETF_LLM_SEED_URL", base)
    index = seed_fetch.load_index(generation.store_url() or "")
    assert index is not None and index.entry("_groups") is not None
    seed_catalog.cache_index(index)
    monkeypatch.setattr(seed_catalog, "refresh_mirror", lambda: None)
    monkeypatch.setattr(corpus_tools, "gather_enabled", lambda: True)
    assert "_groups" not in corpus_tools._available_to_seed([])  # pylint: disable=protected-access


def test_cloud_backend_skips_the_refresh(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    dt = _Datatracker()
    _refresh(dt)
    assert dt.urls == [] and reader.load_groups() is None


def test_embed_settings_resolve_like_a_gather(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    from ietf_llm.gather.sequencer import embed_settings  # pylint: disable=import-outside-toplevel

    assert embed_settings() == (None, True)
    monkeypatch.setenv("IETF_LLM_EMBED_MODEL", "some/model")
    monkeypatch.setenv("IETF_LLM_NO_EMBED", "1")
    assert embed_settings() == ("some/model", False)


def test_the_tools_are_registered() -> None:
    import asyncio  # pylint: disable=import-outside-toplevel

    from mcp.server.mcpserver import MCPServer  # pylint: disable=import-outside-toplevel

    server = MCPServer("t")
    tools.register(server)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"get_group_info", "search_groups", "list_milestones", "group_timeline"} <= names


def test_embedded_milestone_text_does_not_depend_on_todays_date(mirror: _Datatracker) -> None:
    mirror.extra_milestones = [_milestone(16, 1, "Long past", "2001-01-01")]
    _refresh(mirror)
    text = Path(reader.charter_file("httpbis")).read_text()
    assert "Long past" in text and "overdue" not in text
    assert "overdue" in reader.render_group("httpbis")


# --- review follow-ups -------------------------------------------------------


def test_done_filter_excludes_proposed_and_review_milestones(mirror: _Datatracker) -> None:
    mirror.extra_milestones = [
        _milestone(20, 1, "Proposed one", "2099-05-01", state="charter"),
        _milestone(21, 1, "Needs a look", "2099-06-01", state="review"),
    ]
    _refresh(mirror)
    done = reader.render_milestones(status="done")
    assert "Submit semantics" in done
    assert "Proposed one" not in done and "Needs a look" not in done
    opened = reader.render_milestones(status="open")
    assert "Needs a look" in opened and "Proposed one" not in opened
    assert "Proposed one" in reader.render_milestones(status="all")


def test_open_count_agrees_with_the_open_listing(mirror: _Datatracker) -> None:
    mirror.extra_milestones = [_milestone(21, 1, "Needs a look", "2099-06-01", state="review")]
    _refresh(mirror)
    assert "2 open of 3" in reader.render_group("httpbis")
    assert reader.render_milestones(group="httpbis").startswith("**2 milestones**")


def test_unchanged_refresh_does_not_touch_the_embedding_model(
    indexed: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[int] = []
    monkeypatch.setattr(writer, "_index", lambda *a, **k: calls.append(1))
    _refresh(indexed, force=False)
    assert calls == []
    _refresh(indexed, force=True)  # feeds unchanged, nothing re-rendered
    assert calls == []
    indexed.extra_milestones = [_milestone(30, 1, "New", "2099-01-01", time_="2026-03-01T00:00:00Z")]
    _refresh(indexed, force=True)
    assert calls == [1]


def test_missing_index_is_built_even_when_nothing_changed(
    mirror: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[int] = []
    monkeypatch.setattr(writer, "_index", lambda *a, **k: calls.append(1))
    _refresh(mirror, force=False)
    assert calls == [1]


def test_failed_group_list_is_not_retried_at_once(isolated_home: Path) -> None:
    _seed_rfc_mirror(isolated_home)
    dt = _Datatracker()
    dt.fail = True
    _refresh(dt, force=True)
    first = len(dt.urls)
    assert first
    _refresh(dt, force=False)
    assert len(dt.urls) == first
    dt.fail = False
    _refresh(dt, force=True)
    assert reader.load_groups()
    _refresh(dt, force=False)  # recovered: the failure stamp is gone


def test_empty_group_list_does_not_wipe_the_mirror(mirror: _Datatracker) -> None:
    mirror.empty_groups = True
    _refresh(mirror)
    assert reader.get_group("httpbis") is not None
    assert reader.charter_text("httpbis")


def test_recharter_in_progress_shows_the_approved_revision(mirror: _Datatracker) -> None:
    _CHARTER_DOCS[0]["rev"] = "09-02"
    seen: List[str] = []

    def spy(doc: str, rev: str) -> Optional[str]:
        seen.append(rev)
        return _charter_fetch(doc, rev)

    try:
        writer.ensure_groups_index(Verbosity.QUIET, fetch=mirror, charter_fetch=spy, force=True, embed=False)
    finally:
        _CHARTER_DOCS[0]["rev"] = "09"
    group = reader.get_group("httpbis")
    assert group and group["charter_rev"] == "09" and group["charter_status"] == "rechartering"
    assert "working revision 09-02" in reader.render_group("httpbis")
    assert "09-02" not in seen


def test_never_approved_charter_is_flagged(mirror: _Datatracker) -> None:
    _CHARTER_DOCS[1]["rev"] = "00-03"
    try:
        _refresh(mirror)
    finally:
        _CHARTER_DOCS[1]["rev"] = "05"
    group = reader.get_group("l3vpn")
    assert group and group["charter_rev"] == "00-03" and group["charter_status"] == "unapproved"
    assert "never approved" in reader.render_group("l3vpn")
    assert "never approved" in Path(reader.charter_file("l3vpn")).read_text()


def test_approved_state_keeps_a_dashed_revision(mirror: _Datatracker) -> None:
    assert approved_rev({"rev": "20-02", "states": ["/api/v1/doc/state/88/"]}) == ("20-02", "")


def test_not_gathered_message_is_honest_on_cloud(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    assert "cloud backend" in reader.render_group("httpbis")
    assert "next time you run" not in reader.render_group("httpbis")


def test_resolved_args_are_not_merged_twice(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    import argparse  # pylint: disable=import-outside-toplevel

    from ietf_llm import config  # pylint: disable=import-outside-toplevel
    from ietf_llm.gather.sequencer import embed_settings  # pylint: disable=import-outside-toplevel

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("merged again")

    monkeypatch.setattr(config, "merge_global", boom)
    args = argparse.Namespace(embed_model="m", no_embed=False, _global_sources={})
    assert embed_settings(args) == ("m", True)


# --- index retry ------------------------------------------------------------


def _model_availability(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Make the embedding model come and go without undoing other patches
    (which would also undo the sandboxed HOME)."""
    embeddings._MODEL_CACHE["stub"] = _AreaModel()  # pylint: disable=protected-access
    state: Dict[str, Any] = {"model": None}
    monkeypatch.setattr(embeddings, "_get_embed_model", lambda name, verbose: state["model"])
    return state


def test_failed_index_build_is_retried_though_nothing_changed(
    mirror: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    available = _model_availability(monkeypatch)
    _refresh(mirror, embed=True, embed_model="stub", force=False)
    assert not tools.index_installed()
    assert (Path(reader.state_dir()) / "index-pending").exists()
    available["model"] = _AreaModel()
    _refresh(mirror, embed=True, embed_model="stub", force=False)  # no feed or file change
    assert tools.index_installed()
    assert not (Path(reader.state_dir()) / "index-pending").exists()


def test_stale_index_is_rebuilt_after_a_failure_that_left_it_behind(
    indexed: _Datatracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[int] = []
    real = writer._index  # pylint: disable=protected-access
    available = _model_availability(monkeypatch)
    indexed.extra_milestones = [_milestone(40, 1, "Changed", "2099-01-01", time_="2026-04-01T00:00:00Z")]
    _refresh(indexed, embed=True, embed_model="stub", force=True)
    assert (Path(reader.state_dir()) / "index-pending").exists()
    available["model"] = _AreaModel()
    monkeypatch.setattr(writer, "_index", lambda *a, **k: calls.append(1) or real(*a, **k))
    _refresh(indexed, embed=True, embed_model="stub", force=False)
    assert calls == [1]
    assert not (Path(reader.state_dir()) / "index-pending").exists()


def test_nothing_pending_means_no_model_load(indexed: _Datatracker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer, "_index", lambda *a, **k: (_ for _ in ()).throw(AssertionError("loaded")))
    _refresh(indexed, embed=True, embed_model="stub", force=False)


# --- starvation protection --------------------------------------------------


def test_a_charter_that_always_fails_cannot_hold_the_queue() -> None:
    groups = _fake_groups(7)
    poison = {f"charter-ietf-g{i:02d}" for i in range(5)}

    def fetch(doc: str, rev: str) -> Optional[str]:  # noqa: ARG001
        return None if doc in poison else "text"

    fetched: Dict[str, Dict[str, Any]] = {}
    for _ in range(writer._MAX_TRIES):  # pylint: disable=protected-access
        got = writer._fetch_charter_batch(groups, fetched, fetch, 100, Verbosity.QUIET)  # pylint: disable=protected-access
        assert got == {}  # each batch ends on the same five
    got = writer._fetch_charter_batch(groups, fetched, fetch, 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert set(got) == {"g05", "g06"}


def test_a_set_aside_charter_is_tried_again_after_the_lockout() -> None:
    groups = _fake_groups(1)
    now = time.time()
    tries = writer._MAX_TRIES  # pylint: disable=protected-access
    fetched = {"g00": {"rev": "01", "ok": False, "at": now, "tries": tries}}
    assert writer._pending_charters(groups, fetched, now) == []  # pylint: disable=protected-access
    later = now + writer._TRANSIENT_LOCKOUT + 1  # pylint: disable=protected-access
    assert writer._pending_charters(groups, fetched, later) == groups  # pylint: disable=protected-access


def test_fewer_than_the_limit_failures_stay_pending() -> None:
    groups = _fake_groups(1)
    fetched = {"g00": {"rev": "01", "ok": False, "at": time.time(), "tries": 2}}
    assert writer._pending_charters(groups, fetched, time.time()) == groups  # pylint: disable=protected-access


def test_attempt_count_restarts_for_a_new_revision() -> None:
    groups = _fake_groups(1)
    fetched = {"g00": {"rev": "00", "ok": False, "at": time.time(), "tries": 4}}
    writer._fetch_charter_batch(groups, fetched, lambda d, r: None, 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert fetched["g00"]["tries"] == 1 and fetched["g00"]["rev"] == "01"


def test_records_from_before_attempts_were_counted_keep_the_old_window() -> None:
    groups = _fake_groups(1)
    now = time.time()
    legacy = {"g00": {"rev": "01", "ok": False, "at": now}}
    assert writer._pending_charters(groups, legacy, now) == []  # pylint: disable=protected-access
    assert writer._pending_charters(groups, legacy, now + writer._RETRY_AFTER + 1) == groups  # pylint: disable=protected-access


def test_absent_revision_is_recorded_as_absent() -> None:
    fetched: Dict[str, Dict[str, Any]] = {}
    writer._fetch_charter_batch(_fake_groups(1), fetched, lambda d, r: "", 100, Verbosity.QUIET)  # pylint: disable=protected-access
    assert fetched["g00"]["absent"] is True and fetched["g00"]["ok"] is False
