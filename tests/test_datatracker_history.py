"""Tests for Datatracker-sourced governance / lifecycle events.

We stub `_get_json` in `ietf_llm.gather.datatracker` so no HTTP is
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

from ietf_llm.gather import datatracker_history
from ietf_llm.utils import Verbosity


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
                    "type": "added_comment",
                    "desc": "Charter approved by IESG.",
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
    # "added_comment" with no charter wording is just housekeeping.
    _stub_get_json(monkeypatch, {
        "/group/groupevent/": {
            "objects": [
                {
                    "time": _now_minus(5),
                    "type": "added_comment",
                    "desc": "Minor note from secretariat.",
                },
            ],
        },
    })
    events = datatracker_history.fetch_group_events(
        "wg", months=12, verbose=Verbosity.QUIET,
    )
    assert events == []


# --- fetch_role_history ---------------------------------------------------


def test_role_history_returns_chair_appointments_full_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A chair appointment from 10 years ago should still appear —
    # chairs are permanent context, policy (2).
    _stub_get_json(monkeypatch, {
        "/group/rolehistory/": {
            "objects": [
                {
                    "time": _now_minus(365 * 10),
                    "name": "/api/v1/name/rolename/chair/",
                    "person": "/api/v1/person/person/12345/",
                },
            ],
        },
        "/person/person/12345/": {"name": "Mark Nottingham"},
    })
    events = datatracker_history.fetch_role_history(
        "wg", verbose=Verbosity.QUIET,
    )
    assert len(events) == 1
    assert events[0].kind == "chair-appointed"
    assert "Mark Nottingham" in events[0].title


def test_role_history_ignores_non_chair_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADs cycle for all WGs in an area — listing every AD rotation
    # is noise. We deliberately surface chairs only.
    _stub_get_json(monkeypatch, {
        "/group/rolehistory/": {
            "objects": [
                {
                    "time": _now_minus(30),
                    "name": "/api/v1/name/rolename/ad/",
                    "person": "/api/v1/person/person/99/",
                },
            ],
        },
    })
    events = datatracker_history.fetch_role_history(
        "wg", verbose=Verbosity.QUIET,
    )
    assert events == []


def test_role_history_handles_unresolvable_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Person endpoint returns None → skip rather than emit a
    # nameless "Chair appointed: " row.
    _stub_get_json(monkeypatch, {
        "/group/rolehistory/": {
            "objects": [
                {
                    "time": _now_minus(30),
                    "name": "/api/v1/name/rolename/chair/",
                    "person": "/api/v1/person/person/77/",
                },
            ],
        },
        "/person/person/77/": None,
    })
    events = datatracker_history.fetch_role_history(
        "wg", verbose=Verbosity.QUIET,
    )
    assert events == []


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
    from ietf_llm.gather.datatracker_history import _slug_from_url
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
    from ietf_llm.gather.datatracker_history import _parse_dt_time
    parsed = _parse_dt_time("2024-09-12T14:21:33")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2024
    # Malformed input returns None rather than raising.
    assert _parse_dt_time("nope") is None
    assert _parse_dt_time(None) is None
