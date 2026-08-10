"""Tests for the live Datatracker lookups (`ietf_llm.live_lookup`) and the
two MCP tool renderers that consume them (`meeting_schedule`,
`draft_status`).

The single network seam is `live_lookup.cache._fetch_json`; every test stubs it
with canned JSON keyed by URL, so nothing here touches the network. The
canned shapes mirror what Datatracker actually serves (verified against
live `agenda.json` / `doc/document` responses): the agenda's `start` is
UTC, a doc's `states` are URIs whose `type` is *itself* a URI to resolve to
a slug.
"""

from __future__ import annotations
from ietf_llm import mcp

from typing import Any, Dict, Optional

import pytest

from ietf_llm import live_lookup
from ietf_llm.mcp.drafts import tool_draft_status
from ietf_llm.mcp.meetings import tool_meeting_schedule

# --- Canned Datatracker responses -----------------------------------------

# IETF 126 in Vienna (CEST = UTC+2 in July): a 07:00Z start is 09:00 local.
# Two httpbis sessions, to exercise the multi-session requirement, plus a
# session for another group that must be filtered out.
_AGENDA_126 = {
    "126": [
        {
            "group": {"acronym": "httpbis"},
            "start": "2026-07-23T07:00:00Z",
            "duration": "1:30:00",
            "location": "Park Suite 1",
            "session_id": 40001,
            "agenda": "https://datatracker.ietf.org/meeting/126/materials/agenda-126-httpbis-00",
            "minutes": None,
        },
        {
            "group": {"acronym": "httpbis"},
            "start": "2026-07-20T13:00:00Z",
            "duration": "2:00:00",
            "location": "Park Suite 2",
            "session_id": 40002,
            "agenda": None,
            "minutes": None,
        },
        {
            "group": {"acronym": "quic"},
            "start": "2026-07-21T07:00:00Z",
            "duration": "1:00:00",
            "location": "Hall A",
            "session_id": 40003,
        },
    ]
}

_MEETING_126 = {"objects": [{"number": "126", "time_zone": "Europe/Vienna"}]}

# An aipref interim: one session on Meetecho (URL in remote_instructions),
# plus a non-group row that must be filtered out. Same agenda.json shape as a
# numbered meeting, keyed by the interim id.
_INTERIM = "interim-2026-aipref-05"
_AGENDA_INTERIM = {
    _INTERIM: [
        {
            "group": {"acronym": "aipref"},
            "start": "2026-04-15T15:00:00Z",
            "duration": "1:00:00",
            "location": None,
            "session_id": 35177,
            "agenda": (
                "https://datatracker.ietf.org/meeting/"
                f"{_INTERIM}/materials/agenda-{_INTERIM}-aipref-01-00"
            ),
            "minutes": None,
            "remote_instructions": (
                "https://meetings.conf.meetecho.com/interim/?session=35177"
            ),
        },
        {"group": {"acronym": "tls"}, "start": "2026-04-15T15:00:00Z"},
    ]
}
_MEETING_INTERIM = {"objects": [{"number": _INTERIM, "time_zone": "UTC"}]}

# The group's sessions (recent first) + their meetings, for the upcoming list.
# 2099 is future (kept); 126 is past (dropped) relative to a 2026 "today".
_SESSIONS_AIPREF = {
    "meta": {"next": None},
    "objects": [
        {"meeting": "/api/v1/meeting/meeting/4860/"},
        {"meeting": "/api/v1/meeting/meeting/4400/"},
    ],
}
_MEETINGS_BY_ID = {
    "objects": [
        {
            "number": _INTERIM,
            "type": "/api/v1/name/meetingtypename/interim/",
            "date": "2099-04-15",
        },
        {
            "number": "126",
            "type": "/api/v1/name/meetingtypename/ietf/",
            "date": "2020-07-18",
        },
    ]
}

# A document whose states resolve to draft=Active, draft-iesg="I-D Exists".
_DOC_ACTIVE = {
    "name": "draft-ietf-httpbis-foo",
    "rev": "03",
    "expires": "2027-01-01T00:00:00Z",
    "rfc_number": None,
    "intended_std_level": "/api/v1/name/intendedstdlevelname/ps/",
    "states": ["/api/v1/doc/state/1/", "/api/v1/doc/state/2/"],
}

_STATES = {
    "/api/v1/doc/state/1/": {
        "name": "Active",
        "slug": "active",
        "type": "/api/v1/doc/statetype/draft/",
    },
    "/api/v1/doc/state/2/": {
        "name": "I-D Exists",
        "slug": "idexists",
        "type": "/api/v1/doc/statetype/draft-iesg/",
    },
    "/api/v1/doc/state/3/": {
        "name": "RFC Ed Queue",
        "slug": "rfcqueue",
        "type": "/api/v1/doc/statetype/draft-iesg/",
    },
    # The per-stream states — the WG's own process, which a draft in WGLC
    # carries while its IESG state is still "I-D Exists".
    "/api/v1/doc/state/41/": {
        "name": "In WG Last Call",
        "slug": "wg-lc",
        "type": "/api/v1/doc/statetype/draft-stream-ietf/",
    },
    "/api/v1/doc/state/42/": {
        "name": "Submitted to IESG for Publication",
        "slug": "sub-pub",
        "type": "/api/v1/doc/statetype/draft-stream-ietf/",
    },
    "/api/v1/doc/state/43/": {
        "name": "Dead WG Document",
        "slug": "dead",
        "type": "/api/v1/doc/statetype/draft-stream-ietf/",
    },
    "/api/v1/doc/state/44/": {
        "name": "Replaced",
        "slug": "repl",
        "type": "/api/v1/doc/statetype/draft/",
    },
    "/api/v1/doc/state/45/": {
        "name": "WG Document",
        "slug": "wg-doc",
        "type": "/api/v1/doc/statetype/draft-stream-ietf/",
    },
    # The ISE stream, for the two-streams-on-one-document case.
    "/api/v1/doc/state/46/": {
        "name": "Published RFC",
        "slug": "pub",
        "type": "/api/v1/doc/statetype/draft-stream-ise/",
    },
}

_STATE_TYPES = {
    "/api/v1/doc/statetype/draft/": {"slug": "draft"},
    "/api/v1/doc/statetype/draft-iesg/": {"slug": "draft-iesg"},
    "/api/v1/doc/statetype/draft-stream-ietf/": {"slug": "draft-stream-ietf"},
    "/api/v1/doc/statetype/draft-stream-ise/": {"slug": "draft-stream-ise"},
}

_INTENDED = {"/api/v1/name/intendedstdlevelname/ps/": {"name": "Proposed Standard"}}


def _canned(url: str) -> Optional[Dict[str, Any]]:
    """Resolve a stubbed URL to its canned body (or None for an unknown doc)."""
    if "/meeting/126/agenda.json" in url:
        return _AGENDA_126
    if "/meeting/meeting/?number=126" in url:
        return _MEETING_126
    if f"/meeting/{_INTERIM}/agenda.json" in url:
        return _AGENDA_INTERIM
    if f"/meeting/meeting/?number={_INTERIM}" in url:
        return _MEETING_INTERIM
    if "/meeting/session/?group__acronym=aipref" in url:
        return _SESSIONS_AIPREF
    if "/meeting/meeting/?id__in=" in url:
        return _MEETINGS_BY_ID
    if "/doc/document/draft-ietf-httpbis-foo/" in url:
        return _DOC_ACTIVE
    if "/doc/document/" in url:
        return None  # unknown document
    for path, body in {**_STATES, **_STATE_TYPES, **_INTENDED}.items():
        if path in url:
            return body
    return None


@pytest.fixture(autouse=True)
def _stub_fetch(monkeypatch, isolated_home):
    """Replace the one network seam and clear the TTL cache around each test.

    `isolated_home` sandboxes the disk-backed live cache these tools now write
    (`_cached_json` persists a `.live-cache.json` under the cache root), so a
    test never touches the real user cache.
    """
    live_lookup._reset_cache()
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: _canned(url))
    yield
    live_lookup._reset_cache()


# --- Meeting sessions ------------------------------------------------------


def test_meeting_sessions_venue_local_and_both_slots():
    sessions, _, error = live_lookup.fetch_meeting_sessions("httpbis", "126")
    assert error is None
    assert len(sessions) == 2  # both httpbis slots; quic filtered out
    first = sessions[0]
    # 07:00Z in Vienna in July is 09:00 CEST, +90min -> 10:30.
    assert first.start_local == "09:00"
    assert first.end_local == "10:30"
    assert first.tz == "Europe/Vienna"
    assert first.tz_abbrev == "CEST"
    assert first.start_utc == "2026-07-23T07:00Z"
    assert first.room == "Park Suite 1"
    assert first.date == "Thursday 23 July 2026"


def test_meeting_sessions_meetecho_urls():
    sessions, _, _ = live_lookup.fetch_meeting_sessions("httpbis", "126")
    first = sessions[0]
    assert first.meetecho_full.endswith("/ietf126/?session=40001")
    assert first.meetecho_onsite.endswith("/onsite126/?session=40001")


def test_meeting_sessions_no_session_for_group():
    sessions, _, error = live_lookup.fetch_meeting_sessions("tls", "126")
    assert error is None
    assert sessions == []


def test_meeting_sessions_no_agenda(monkeypatch):
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: None)
    sessions, _, error = live_lookup.fetch_meeting_sessions("httpbis", "999")
    assert sessions == []
    assert error and "999" in error


def test_meeting_sessions_renders_both():
    out = tool_meeting_schedule("httpbis", "126")
    assert "2 session(s)" in out
    assert "09:00–10:30 CEST" in out
    assert "/ietf126/?session=40001" in out
    assert "Live from Datatracker" in out


def test_meeting_sessions_interim_uses_remote_instructions():
    sessions, _, error = live_lookup.fetch_meeting_sessions("aipref", _INTERIM)
    assert error is None
    assert len(sessions) == 1  # the tls row is filtered out
    sess = sessions[0]
    # Interims carry connection details in remote_instructions; no onsite room
    # and no constructed Meetecho pair.
    assert sess.remote_instructions.endswith("/interim/?session=35177")
    assert sess.meetecho_full == ""
    assert sess.meetecho_onsite == ""


def test_meeting_sessions_renders_interim():
    out = tool_meeting_schedule("aipref", _INTERIM)
    assert f"# aipref at {_INTERIM}" in out
    assert "/interim/?session=35177" in out
    assert "Meetecho (onsite)" not in out
    assert "Live from Datatracker" in out


def test_meeting_sessions_rejects_garbage_meeting():
    out = tool_meeting_schedule("httpbis", "next week")
    assert "interim id" in out


def test_meeting_sessions_interim_id_is_case_insensitive():
    # A mixed-case interim id passes validation and must still resolve against
    # Datatracker's lowercase key, not 404 as "no agenda".
    sessions, _, error = live_lookup.fetch_meeting_sessions(
        "aipref", _INTERIM.upper()
    )
    assert error is None
    assert len(sessions) == 1


def test_upcoming_meetings_lists_future_only():
    meetings, _, error = live_lookup.fetch_upcoming_meetings("aipref")
    assert error is None
    # Only the future-dated interim survives; the 2020 numbered meeting is past.
    assert [m.number for m in meetings] == [_INTERIM]
    assert meetings[0].kind == "interim"


def test_upcoming_meetings_rendered_when_meeting_omitted():
    out = tool_meeting_schedule("aipref")
    assert "upcoming meeting(s)" in out
    assert _INTERIM in out
    assert "Live from Datatracker" in out


# --- Draft status ----------------------------------------------------------


def test_draft_status_active_is_in_wg():
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo-03")
    assert status is not None
    assert status.found
    assert status.draft_state == "Active"
    assert status.iesg_state == "I-D Exists"
    assert status.intended_status == "Proposed Standard"
    assert status.eligibility == "in-wg"


def test_draft_status_unknown_doc():
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-nope")
    assert status is None


def test_draft_status_rfc_ed_queue_is_in_iesg(monkeypatch):
    doc = dict(_DOC_ACTIVE, states=["/api/v1/doc/state/1/", "/api/v1/doc/state/3/"])
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: doc if "/doc/document/" in url else _canned(url),
    )
    live_lookup._reset_cache()
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.iesg_state == "RFC Ed Queue"
    assert status.eligibility == "in-iesg"


def test_draft_status_published_by_rfc_number(monkeypatch):
    doc = dict(_DOC_ACTIVE, rfc_number=9999, states=[])
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: doc if "/doc/document/" in url else _canned(url),
    )
    live_lookup._reset_cache()
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.eligibility == "published"
    assert status.rfc_number == "9999"


def test_draft_status_empty_states_corroborated_dead(monkeypatch):
    # No states AND an expiry in the past -> dead, with a corroboration note.
    doc = dict(_DOC_ACTIVE, states=[], expires="2000-01-01T00:00:00Z")
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: doc if "/doc/document/" in url else _canned(url),
    )
    live_lookup._reset_cache()
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.eligibility == "dead"
    assert status.note and "no states" in status.note.lower()


def _stub_doc(monkeypatch, **overrides):
    """Serve `_DOC_ACTIVE` with overrides for the doc fetch; canned elsewhere."""
    doc = dict(_DOC_ACTIVE, **overrides)
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: doc if "/doc/document/" in url else _canned(url),
    )
    live_lookup._reset_cache()


def test_draft_status_reports_wg_last_call(monkeypatch):
    # The regression: a draft in WGLC carries a draft-stream-ietf state of
    # "In WG Last Call" while its IESG state is still "I-D Exists". Reading
    # only the draft/draft-iesg states loses the WGLC entirely.
    _stub_doc(
        monkeypatch,
        states=[
            "/api/v1/doc/state/1/",
            "/api/v1/doc/state/2/",
            "/api/v1/doc/state/41/",
        ],
    )
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.stream == "ietf"
    assert status.stream_state == "In WG Last Call"
    assert status.stream_state_slug == "wg-lc"
    assert status.iesg_state == "I-D Exists"
    assert status.eligibility == "in-wg"  # WGLC is still the WG's document


def test_draft_status_renders_wg_state(monkeypatch):
    _stub_doc(
        monkeypatch,
        states=[
            "/api/v1/doc/state/1/",
            "/api/v1/doc/state/2/",
            "/api/v1/doc/state/41/",
        ],
    )
    out = tool_draft_status("draft-ietf-httpbis-foo")
    assert "WG state:** In WG Last Call" in out
    assert "IESG state:** I-D Exists" in out


def test_draft_status_submitted_to_iesg_is_in_iesg(monkeypatch):
    # Handed to the IESG but not yet picked up: the IESG state is still
    # "I-D Exists", so only the stream state says it has left the WG.
    _stub_doc(
        monkeypatch,
        states=[
            "/api/v1/doc/state/1/",
            "/api/v1/doc/state/2/",
            "/api/v1/doc/state/42/",
        ],
    )
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.stream_state == "Submitted to IESG for Publication"
    assert status.eligibility == "in-iesg"


def test_draft_status_dead_wg_document_is_dead(monkeypatch):
    _stub_doc(
        monkeypatch,
        states=[
            "/api/v1/doc/state/1/",
            "/api/v1/doc/state/2/",
            "/api/v1/doc/state/43/",
        ],
    )
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.eligibility == "dead"


def test_draft_status_non_ietf_stream_labelled_generically(monkeypatch):
    # An IRTF/IAB/ISE doc's stream state is not a "WG state"; the renderer
    # must not call it one.
    monkeypatch.setitem(
        _STATE_TYPES,
        "/api/v1/doc/statetype/draft-stream-irtf/",
        {"slug": "draft-stream-irtf"},
    )
    monkeypatch.setitem(
        _STATES,
        "/api/v1/doc/state/47/",
        {
            "name": "In RG Last Call",
            "slug": "rg-lc",
            "type": "/api/v1/doc/statetype/draft-stream-irtf/",
        },
    )
    _stub_doc(monkeypatch, states=["/api/v1/doc/state/1/", "/api/v1/doc/state/47/"])
    out = tool_draft_status("draft-ietf-httpbis-foo")
    assert "RG state:** In RG Last Call" in out
    assert "WG state" not in out


def test_draft_status_two_streams_uses_the_docs_own_stream(monkeypatch):
    # `draft-eastlake-fnv` really does carry both a draft-stream-ietf state
    # (`sub-pub`, from an abandoned IETF-stream attempt) and a
    # draft-stream-ise one. `states[]` order is not a contract, so the doc's
    # own `stream` field decides — last-one-wins would report the wrong
    # stream and, here, derive `in-iesg` off a stale `sub-pub`.
    _stub_doc(
        monkeypatch,
        stream="/api/v1/name/streamname/ise/",
        states=[
            "/api/v1/doc/state/1/",
            "/api/v1/doc/state/46/",  # draft-stream-ise: Published RFC
            "/api/v1/doc/state/42/",  # draft-stream-ietf: sub-pub (stale)
        ],
    )
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status.stream == "ise"
    assert status.stream_state == "Published RFC"
    assert status.eligibility == "in-wg"  # not in-iesg off the stale sub-pub


def test_eligibility_iesg_dead_maps_to_dead():
    # A draft parked in IESG state "Dead" is dead, not in-iesg processing.
    assert (
        live_lookup._derive_eligibility("Active", "Dead", "2027-01-01T00:00:00Z", None)
        == "dead"
    )


def test_draft_status_renders():
    out = tool_draft_status("draft-ietf-httpbis-foo")
    assert "draft-ietf-httpbis-foo" in out
    assert "IESG state:** I-D Exists" in out
    assert "agenda-eligible" in out
    assert "Live from Datatracker" in out


# --- Cache + stamp ---------------------------------------------------------


def test_ttl_cache_fetches_once(monkeypatch):
    calls = {"n": 0}

    def _counting(url, timeout=10.0):
        calls["n"] += 1
        return _canned(url)

    monkeypatch.setattr(live_lookup.cache, "_fetch_json", _counting)
    live_lookup._reset_cache()
    live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    after_first = calls["n"]
    live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    # Second call within the TTL re-uses every cached URL — no new fetches.
    assert calls["n"] == after_first


def test_disk_cache_serves_cold_process(monkeypatch):
    # The load-bearing protection for short-lived processes (e.g. a restarted
    # stdio subprocess): a fresh (cold) process starts with an empty in-process
    # cache but must reuse a recent fetch from disk, not re-hit Datatracker.
    calls = []
    url = "https://datatracker.ietf.org/api/v1/doc/document/?name=x"
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda u, timeout=10.0: calls.append(u) or {"ok": True},
    )
    body1, _ = live_lookup._cached_json(url)
    assert body1 == {"ok": True} and len(calls) == 1
    live_lookup._reset_cache()  # simulate a new, cold process
    body2, _ = live_lookup._cached_json(url)
    assert body2 == {"ok": True}
    assert len(calls) == 1  # served from the disk cache; no second fetch


def test_disk_cache_stale_fallback_on_cold_process(monkeypatch):
    # A cold process whose only prior data is an EXPIRED disk entry, with
    # Datatracker down, still returns the stale answer rather than nothing.
    url = "https://datatracker.ietf.org/api/v1/doc/document/?name=z"
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda u, timeout=10.0: {"ok": 1})
    live_lookup._cached_json(url)  # populate the disk cache
    live_lookup._reset_cache()  # cold process: empty in-process cache
    monkeypatch.setenv("IETF_LLM_LIVE_TTL", "0")  # the disk entry is now stale
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda u, timeout=10.0: None)  # down
    body, _ = live_lookup._cached_json(url)
    assert body == {"ok": 1}  # served the stale disk datum, not None


def test_disk_get_rejects_non_dict_body():
    # A corrupt / hand-edited entry with a non-dict body is a miss, not a
    # None "hit" that would mask a healthy fetch or cache None in-process.
    import json
    import os
    import time

    path = live_lookup._live_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {"u": {"body": None, "epoch": time.time(), "fetched_at": "2026-01-01T00:00:00+00:00"}},
            fh,
        )
    assert live_lookup._disk_get("u", 300) is None


def test_stale_fallback_on_fetch_failure(monkeypatch):
    live_lookup._reset_cache()
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: _canned(url))
    live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    # TTL 0 forces re-fetch; the seam now fails -> stale cache is returned.
    monkeypatch.setenv("IETF_LLM_LIVE_TTL", "0")
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: None)
    status, _ = live_lookup.fetch_draft_status("draft-ietf-httpbis-foo")
    assert status is not None and status.draft_state == "Active"


# --- Overview reconciliation -----------------------------------------------

# Reconciliation derives eligibility straight from the group listing's
# per-draft `states`, so each object carries them (state 1=Active draft,
# 2=I-D Exists iesg, 3=RFC Ed Queue iesg — from _STATES above).
#   foo = in-WG (matches); bar = advanced (RFC Ed Queue); baz = a revived
#   adopted draft (in-WG on Datatracker, absent from the cache's active list);
#   old = expired adopted draft (past the WG, must NOT read as revived).
def _adopted(name, states, expires):
    return {"name": name, "rfc_number": None, "states": states, "expires": expires}


_IN_WG = ["/api/v1/doc/state/1/", "/api/v1/doc/state/2/"]
_PAST_WG = ["/api/v1/doc/state/1/", "/api/v1/doc/state/3/"]
_GROUP_DRAFTS = {
    "meta": {"next": None},
    "objects": [
        _adopted("draft-ietf-httpbis-foo", _IN_WG, "2027-01-01T00:00:00Z"),
        _adopted("draft-ietf-httpbis-bar", _PAST_WG, "2027-01-01T00:00:00Z"),
        _adopted("draft-ietf-httpbis-baz", _IN_WG, "2027-06-01T00:00:00Z"),
        # An individual draft associated with the group — ignored (prefix).
        _adopted("draft-someone-httpbis-idea", _IN_WG, "2027-06-01T00:00:00Z"),
        # An adopted draft whose expiry is past — dead, not revived.
        _adopted("draft-ietf-httpbis-old", _IN_WG, "2000-01-01T00:00:00Z"),
    ],
}


def _canned_recon(url: str):
    if "group__acronym=httpbis&type=draft" in url:
        return _GROUP_DRAFTS
    return _canned(url)


def test_reconcile_flags_advanced_and_revived(monkeypatch):
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: _canned_recon(url))
    live_lookup._reset_cache()
    recon, _ = live_lookup.reconcile_active_drafts(
        "httpbis", ["draft-ietf-httpbis-foo", "draft-ietf-httpbis-bar"]
    )
    assert recon.checked == 2
    advanced_names = [n for n, _ in recon.advanced]
    assert advanced_names == ["draft-ietf-httpbis-bar"]  # foo stays in-WG
    revived_names = [n for n, _ in recon.revived]
    # baz revived; the individual draft (prefix) and the expired one are out.
    assert revived_names == ["draft-ietf-httpbis-baz"]


def test_reconcile_advanced_label_pairs_process_and_draft_state(monkeypatch):
    # Either half alone misleads: "I-D Exists" says nothing about a replaced
    # draft, and a bare "WG Document (dead)" reads as a contradiction — the
    # Replaced is the part that made it dead. Pair them.
    drafts = {
        "meta": {"next": None},
        "objects": [
            # Replaced, still a WG Document on the stream side.
            _adopted(
                "draft-ietf-httpbis-repl",
                [
                    "/api/v1/doc/state/44/",
                    "/api/v1/doc/state/2/",
                    "/api/v1/doc/state/45/",
                ],
                "2027-01-01T00:00:00Z",
            ),
            # A real IESG state: "Active" explains nothing, so it is dropped.
            _adopted(
                "draft-ietf-httpbis-iesg", _PAST_WG, "2027-01-01T00:00:00Z"
            ),
        ],
    }

    def _stub(url, timeout=10.0):
        if "group__acronym=httpbis&type=draft" in url:
            return drafts
        return _canned(url)

    monkeypatch.setattr(live_lookup.cache, "_fetch_json", _stub)
    live_lookup._reset_cache()
    recon, _ = live_lookup.reconcile_active_drafts(
        "httpbis", ["draft-ietf-httpbis-repl", "draft-ietf-httpbis-iesg"]
    )
    labels = dict(recon.advanced)
    assert labels["draft-ietf-httpbis-repl"] == "WG Document / Replaced (dead)"
    assert labels["draft-ietf-httpbis-iesg"] == "RFC Ed Queue (in-iesg)"


def test_reconcile_revived_requires_in_wg_not_just_future_expiry(monkeypatch):
    # A draft absent from the cache's active set but PAST the WG on Datatracker
    # (future expiry, RFC Ed Queue) must NOT be reported as revived — the bug
    # the listing-based, eligibility-confirmed reconciliation fixes.
    drafts = {
        "meta": {"next": None},
        "objects": [
            _adopted("draft-ietf-httpbis-advanced", _PAST_WG, "2027-01-01T00:00:00Z"),
        ],
    }

    def _stub(url, timeout=10.0):
        if "group__acronym=httpbis&type=draft" in url:
            return drafts
        return _canned(url)

    monkeypatch.setattr(live_lookup.cache, "_fetch_json", _stub)
    live_lookup._reset_cache()
    recon, _ = live_lookup.reconcile_active_drafts("httpbis", [])
    assert recon.revived == []  # future expiry alone is not enough


def test_reconcile_clean_when_aligned(monkeypatch):
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", lambda url, timeout=10.0: _canned_recon(url))
    live_lookup._reset_cache()
    # foo and baz are exactly Datatracker's active adopted set; foo is in-WG.
    recon, _ = live_lookup.reconcile_active_drafts(
        "httpbis", ["draft-ietf-httpbis-foo", "draft-ietf-httpbis-baz"]
    )
    assert recon.advanced == []
    assert recon.revived == []
