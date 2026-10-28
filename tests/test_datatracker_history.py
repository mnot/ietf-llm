"""Tests for Datatracker-sourced governance / lifecycle events.

We stub `_get_json` in `ietf_llm.gather.sources.datatracker` so no HTTP is
hit. Tests cover:

- Parsing of group state vs charter events
- Always-include policy for charter and chair events (months ignored)
- `--months` cutoff for document lifecycle events
- Defensive handling of missing / malformed API responses
- Type mapping for document lifecycle slugs
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import datatracker_history
from ietf_llm.log import Verbosity


def _now_minus(days: int) -> str:
    """Datatracker `time` field format, dated `days` ago."""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _stub_get_json(
    monkeypatch: pytest.MonkeyPatch,
    responses: Dict[str, Optional[Dict[str, Any]]],
) -> List[str]:
    """Patch _get_json to return canned responses keyed by URL substring.

    Returns a list that the test can inspect to verify which URLs were
    called. The match is substring-based so tests don't have to encode
    the full query string.
    """
    called: List[str] = []

    def fake(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        called.append(path_or_url)
        for key, body in responses.items():
            if key in path_or_url:
                return body
        return None

    monkeypatch.setattr(datatracker_history, "_get_json", fake)
    return called


# --- fetch_group_events ---------------------------------------------------


def test_group_events_returns_charter_regardless_of_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 5-year-old charter approval must still appear under the default
    # 12-month window — policy (2).
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {
            "objects": [
                {
                    "time": _now_minus(365 * 5),
                    "type": "changed_state",
                    "desc": "Charter approved, group active",
                },
            ],
        },
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert len(events) == 1
    assert events[0].kind == "charter-approved"


def test_group_events_filters_state_change_to_months_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Group state change events should respect --months. A 5-year-old
    # state change is dropped; a recent one is kept.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {
            "objects": [
                {
                    "time": _now_minus(365 * 5),
                    "type": "changed_state",
                    "desc": "State changed to Active.",
                },
                {
                    "time": _now_minus(30),
                    "type": "changed_state",
                    "desc": "State changed to Concluded.",
                },
            ],
        },
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert len(events) == 1
    assert events[0].kind == "group-state"
    assert "Concluded" in events[0].title


def test_group_events_handles_missing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Datatracker unreachable / 404 → empty list, never raises.
    _stub_get_json(monkeypatch, {"/group/groupevent/": None})
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


def test_group_events_ignores_uninteresting_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An info_changed row that is neither a chair nor a charter change.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {
            "objects": [
                {
                    "time": _now_minus(5),
                    "type": "info_changed",
                    "desc": "Resources changed to <b>webpage https://example.org/</b>",
                },
            ],
        },
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


def test_group_events_query_only_the_types_it_classifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _stub_get_json(monkeypatch, {"/group/groupevent/": {"objects": []}})
    datatracker_history.fetch_group_events("wg", months=12, verbose=Verbosity.QUIET)
    assert "type__in=info_changed,changed_state" in called[0]


# --- chair appointments (group events) -----------------------------------


def _chair_event(days: int, desc: str, type_slug: str = "info_changed") -> Dict[str, Any]:
    # Shape of a live /group/groupevent/ row (httpbis, tls).
    return {
        "id": 1,
        "group": "/api/v1/group/group/1/",
        "time": _now_minus(days),
        "type": type_slug,
        "desc": desc,
        "by": "/api/v1/person/person/1/",
    }


def _chair_titles(events: List[Any]) -> List[str]:
    return [e.title for e in events if e.kind == "chair-appointed"]


def test_chair_change_emits_only_the_newly_appointed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30,
            "Chairs changed to <b>Mark Nottingham, Patrick McManus</b> "
            "from Mark Nottingham",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == ["Chair appointed: Patrick McManus"]


def test_chair_events_ignore_months_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Who chaired and when is permanent context, like charter approvals.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            365 * 10, "Chair changed to <b>A B, C D</b> from A B",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == ["Chair appointed: C D"]


def test_chair_change_with_no_prior_chairs_appoints_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30, "Chairs changed to <b>A B, C D</b> from ",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == ["Chair appointed: A B", "Chair appointed: C D"]


def test_chair_departure_is_reported_once_and_appoints_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A name repeated in the old list (seen in real tls data) must not
    # make an existing chair look new or departed.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [
            _chair_event(30, "Chairs changed to <b>A B</b> from A B, C D"),
            _chair_event(60, "Chairs changed to <b>A B, C D</b> from A B, C D, A B"),
        ]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == []
    assert [(e.kind, e.title) for e in events] == [
        ("chair-departed", "Chair stepped down: C D"),
    ]


def test_replacing_a_chair_reports_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tls, 2023: one chair replaced by another in a single change.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30,
            "Chair changed to <b>Deirdre Connolly, Joseph Salowey</b> "
            "from Christopher Wood, Joseph Salowey",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert [(e.kind, e.title) for e in events] == [
        ("chair-appointed", "Chair appointed: Deirdre Connolly"),
        ("chair-departed", "Chair stepped down: Christopher Wood"),
    ]


def test_chair_names_use_the_canonical_spelling_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30, "Chairs changed to <b>joseph salowey</b> from Old Chair",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
        canonical_name=lambda n: "Joseph Salowey" if n == "joseph salowey" else n,
    )
    assert [e.title for e in events] == [
        "Chair appointed: Joseph Salowey",
        "Chair stepped down: Old Chair",
    ]


def test_chair_names_are_unescaped_and_compared_unescaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live descriptions HTML-escape names, and the two sides can use
    # different encodings of the same character.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30,
            "Chairs changed to <b>Karen O&#x27;Donoghue, A B</b> "
            "from Karen O&#39;Donoghue",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == ["Chair appointed: A B"]


def test_chair_change_without_from_clause_still_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [
            _chair_event(30, "Chair changed to <b>A B</b>"),
        ]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert _chair_titles(events) == ["Chair appointed: A B"]


def test_emptying_the_chair_list_reports_only_departures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [
            _chair_event(30, "Chairs changed to <b></b> from A B, C D"),
        ]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert [e.title for e in events] == [
        "Chair stepped down: A B",
        "Chair stepped down: C D",
    ]


def test_other_info_changes_are_not_chair_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADs cycle for every WG in an area; listing them is noise.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [
            _chair_event(30, "Area Director changed to <b>Mike Bishop</b> "
                         "from Francesca Palombini"),
            _chair_event(31, "Info Changed"),
            _chair_event(32, "Chairs changed to <b>A B</b> from ",
                         type_slug="added_comment"),
        ]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


def test_group_events_follow_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chair and charter events are the oldest rows, so they sit on the
    # last page of a busy group.
    page1 = {
        "meta": {"next": "/api/v1/group/groupevent/?offset=500"},
        "objects": [_chair_event(5, "sent scheduled notification", "sent_notification")],
    }
    page2 = {
        "meta": {"next": None},
        "objects": [_chair_event(
            365 * 9, "Chairs changed to <b>A B</b> from "
        )],
    }
    called: List[str] = []

    def fake(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        called.append(path_or_url)
        return page2 if "offset=500" in path_or_url else page1

    monkeypatch.setattr(datatracker_history, "_get_json", fake)
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert len(called) == 2
    assert _chair_titles(events) == ["Chair appointed: A B"]


# --- fetch_doc_events -----------------------------------------------------


def test_doc_events_maps_lifecycle_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/doc/docevent/": {
            "objects": [
                {
                    "time": _now_minus(60),
                    "type": "started_iesg_process",
                    "doc": "/api/v1/doc/document/draft-ietf-aipref-vocab/",
                    "desc": "IESG processing started.",
                },
                {
                    "time": _now_minus(30),
                    "type": "published_rfc",
                    "doc": "/api/v1/doc/document/draft-ietf-aipref-vocab/",
                    "desc": "Published as RFC 9999.",
                },
            ],
        },
    })
    events = datatracker_history.fetch_doc_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    kinds = sorted(e.kind for e in events)
    assert kinds == ["doc-iesg", "doc-rfc"]
    rfc_event = next(e for e in events if e.kind == "doc-rfc")
    assert "draft-ietf-aipref-vocab" in rfc_event.title


def test_doc_events_detects_adoption_from_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adoption arrives as a state-change event with descriptive text,
    # not a dedicated type slug. Verify the desc-based classifier.
    _stub_get_json(monkeypatch, {
        "/doc/docevent/": {
            "objects": [
                {
                    "time": _now_minus(45),
                    "type": "changed_state",
                    "doc": "/api/v1/doc/document/draft-foo/",
                    "desc": "Draft adopted by working group.",
                },
            ],
        },
    })
    events = datatracker_history.fetch_doc_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert len(events) == 1
    assert events[0].kind == "doc-adopted"


def test_doc_events_tell_ietf_last_call_from_wg_last_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real shapes: the IESG's announcement is `sent_last_call`; a WG Last
    # Call is the stream state moving to "In WG Last Call" (httpbis).
    doc = "/api/v1/doc/document/draft-ietf-wg-x/"
    _stub_get_json(monkeypatch, {
        "/doc/docevent/": {"objects": [
            {"time": _now_minus(5), "type": "sent_last_call", "doc": doc,
             "desc": "The following Last Call announcement was sent out "
                     "(ends 2026-09-11):<br><br>From: The IESG"},
            {"time": _now_minus(40), "type": "changed_state", "doc": doc,
             "desc": "IETF WG state changed to <b>In WG Last Call</b> from "
                     "WG Document"},
            {"time": _now_minus(20), "type": "changed_state", "doc": doc,
             "desc": "IETF WG state changed to <b>WG Consensus: Waiting for "
                     "Write-Up</b> from In WG Last Call"},
            {"time": _now_minus(10), "type": "changed_state", "doc": doc,
             "desc": "IESG state changed to <b>In Last Call</b> from Last "
                     "Call Requested"},
        ]},
    })
    events = datatracker_history.fetch_doc_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert sorted((e.kind, e.title) for e in events) == [
        ("doc-lc", "`draft-ietf-wg-x`: IETF Last Call announced"),
        ("doc-wglc", "`draft-ietf-wg-x`: WG Last Call issued"),
    ]


def test_doc_events_respects_months_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An RFC published 5 years ago should NOT appear under months=12 —
    # docevents are high-volume and the cutoff is what keeps the
    # digest tractable for long-running WGs.
    _stub_get_json(monkeypatch, {
        "/doc/docevent/": {
            "objects": [
                {
                    "time": _now_minus(365 * 5),
                    "type": "published_rfc",
                    "doc": "/api/v1/doc/document/draft-old/",
                    "desc": "Published as RFC 1234.",
                },
            ],
        },
    })
    events = datatracker_history.fetch_doc_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


def test_doc_events_handles_missing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {"/doc/docevent/": None})
    events = datatracker_history.fetch_doc_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


# --- helpers --------------------------------------------------------------


def test_slug_from_url_extracts_final_segment() -> None:
    from ietf_llm.gather.sources.datatracker_history import _slug_from_url
    assert _slug_from_url(
        "/api/v1/doc/document/draft-ietf-aipref-vocab/"
    ) == "draft-ietf-aipref-vocab"
    # Expected-section guard rejects mismatched URLs.
    assert _slug_from_url(
        "/api/v1/doc/document/x/", expected_section="rolename"
    ) is None
    # Non-strings are tolerated.
    assert _slug_from_url(None) is None  # type: ignore[arg-type]


def test_parse_dt_time_attaches_utc() -> None:
    from ietf_llm.gather.sources.datatracker_history import _parse_dt_time
    parsed = _parse_dt_time("2024-09-12T14:21:33")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2024
    # Malformed input returns None rather than raising.
    assert _parse_dt_time("nope") is None
    assert _parse_dt_time(None) is None


def test_milestone_events_quoting_the_charter_are_not_charter_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real ntp row: the milestone title mentions the charter.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            365 * 5,
            'Deleted milestone "NTP WG Charter Approved", not present in '
            "approved charter",
            type_slug="changed_milestone",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


def test_group_state_title_has_markup_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {"objects": [_chair_event(
            30,
            "State changed to <b>Concluded</b> from Active",
            type_slug="changed_state",
        )]},
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert [e.title for e in events] == [
        "Group state: State changed to Concluded from Active"
    ]


def test_a_failed_later_page_logs_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    page1 = {
        "meta": {"next": "/api/v1/group/groupevent/?offset=500"},
        "objects": [_chair_event(5, "x", "sent_notification")],
    }

    def fake(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        return None if "offset=500" in path_or_url else page1

    messages: List[str] = []
    monkeypatch.setattr(
        datatracker_history, "log",
        lambda msg, *a, **kw: messages.append(msg),
    )
    monkeypatch.setattr(datatracker_history, "_get_json", fake)
    datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert any("stopped paging" in m for m in messages)
    del capsys
