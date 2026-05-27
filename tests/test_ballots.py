"""Tests for IESG ballot gathering and rendering.

The Datatracker calls themselves are mocked at the `_get_json` seam
in `gather.ballots`, so the tests exercise scoping, position
collapse (latest-event-per-AD wins), and rendering without hitting
the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ietf_llm.gather import ballots as ballots_module
from ietf_llm.gather.ballots import (
    Ballot,
    Position,
    ballot_events,
    fetch_ballots,
    render_ballot,
    write_ballot_files,
)
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir


# --- Mock helpers ---------------------------------------------------------


def _stub_get_json(
    monkeypatch: pytest.MonkeyPatch,
    responses: Dict[str, Optional[Dict[str, Any]]],
) -> Any:
    """Install a stub `_get_json` that returns a canned response per
    URL substring match. The first key the URL contains wins; this
    keeps test fixtures readable (we don't have to reproduce the full
    Datatracker URL including format=json appendages).
    """
    calls: list[str] = []

    def fake(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        # noqa: ARG001
        del timeout
        calls.append(url)
        for needle, body in responses.items():
            if needle in url:
                return body
        return None

    monkeypatch.setattr(ballots_module, "_get_json", fake)
    return calls


def _position_event(
    doc_name: str,
    person_id: int,
    pos_slug: str,
    when_iso: str,
    *,
    discuss: str = "",
    comment: str = "",
    rev: str = "12",
) -> Dict[str, Any]:
    """Shape one ballotpositiondocevent JSON object."""
    return {
        "doc": f"/api/v1/doc/document/{doc_name}/",
        "balloter": f"/api/v1/person/person/{person_id}/",
        "pos": f"/api/v1/name/ballotpositionname/{pos_slug}/",
        "time": when_iso,
        "rev": rev,
        "discuss": discuss,
        "comment": comment,
    }


# --- Scoping --------------------------------------------------------------


def test_drafts_with_no_in_window_activity_are_skipped(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty in-window event list → no drafts in scope → no ballots
    # fetched (no per-doc calls).
    calls = _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": []},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out == []
    # Only the scoping query was issued; no per-doc fetches.
    assert len(calls) == 1


def test_drafts_with_in_window_activity_get_full_ballot_fetched(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scoping query returns events on one draft; the gather then
    # fetches that draft's full ballot.
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    person_url = "/api/v1/person/person/101/"

    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {
            "objects": [
                _position_event("draft-foo", 101, "noobj", recent),
            ],
        },
        "doc__name=draft-foo": {
            "objects": [
                _position_event("draft-foo", 101, "noobj", recent),
            ],
        },
        person_url: {"name": "Alice Chen"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert len(out) == 1
    assert out[0].doc_name == "draft-foo"
    assert len(out[0].positions) == 1
    assert out[0].positions[0].name == "Alice Chen"
    assert out[0].positions[0].pos_slug == "noobj"


# --- Latest-event-per-AD collapse ----------------------------------------


def test_latest_event_per_balloter_wins(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AD updates from DISCUSS to No Objection. The current ballot must
    # show ONLY the No Objection — the older DISCUSS event no longer
    # represents Alice's position.
    now = datetime.now(timezone.utc)
    early = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    late = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {
            "objects": [
                _position_event("draft-foo", 101, "noobj", late),
            ],
        },
        "doc__name=draft-foo": {
            "objects": [
                _position_event(
                    "draft-foo", 101, "discuss", early,
                    discuss="My old objection.",
                ),
                _position_event(
                    "draft-foo", 101, "noobj", late,
                    comment="OK after revision.",
                ),
            ],
        },
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert len(out) == 1
    positions = out[0].positions
    assert len(positions) == 1
    assert positions[0].pos_slug == "noobj"
    # The stale DISCUSS text must NOT survive into the current view.
    assert positions[0].discuss == ""


def test_pre_window_discuss_still_appears_when_ad_hasnt_revisited(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap is on drafts (does this draft have ANY in-window
    # activity?), not on individual positions. A 13-month-old DISCUSS
    # that's still standing must show up if some OTHER AD's position
    # was updated in the window.
    now = datetime.now(timezone.utc)
    long_ago = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {
            "objects": [
                # An in-window event on a DIFFERENT balloter brings the
                # draft into scope.
                _position_event("draft-foo", 202, "noobj", recent),
            ],
        },
        "doc__name=draft-foo": {
            "objects": [
                _position_event(
                    "draft-foo", 101, "discuss", long_ago,
                    discuss="Still standing.",
                ),
                _position_event("draft-foo", 202, "noobj", recent),
            ],
        },
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/202/": {"name": "Bob Smith"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    positions_by_name = {p.name: p for p in out[0].positions}
    assert "Alice Chen" in positions_by_name
    assert positions_by_name["Alice Chen"].pos_slug == "discuss"
    assert "Still standing." in positions_by_name["Alice Chen"].discuss


# --- Rendering -----------------------------------------------------------


def _make_ballot(*positions: Position) -> Ballot:
    return Ballot(
        doc_name="draft-foo",
        positions=list(positions),
        first_event=datetime(2026, 1, 1, tzinfo=timezone.utc),
        latest_in_window=datetime(2026, 5, 20, tzinfo=timezone.utc),
        latest_rev="12",
    )


def _pos(name: str, slug: str, **kwargs: Any) -> Position:
    return Position(
        person_url=f"/api/v1/person/person/{abs(hash(name)) % 1000}/",
        name=name,
        pos_slug=slug,
        pos_label={
            "discuss": "DISCUSS", "noobj": "No Objection",
            "yes": "Yes", "abstain": "Abstain", "recuse": "Recuse",
        }[slug],
        when=kwargs.get("when", datetime(2026, 5, 1, tzinfo=timezone.utc)),
        rev=kwargs.get("rev", "12"),
        comment=kwargs.get("comment", ""),
        discuss=kwargs.get("discuss", ""),
    )


def test_render_ballot_puts_discuss_first() -> None:
    ballot = _make_ballot(
        _pos("Alice Chen", "noobj"),
        _pos("Bob Smith", "discuss", discuss="My DISCUSS body."),
        _pos("Carol Lee", "yes"),
    )
    text = render_ballot(ballot)
    discuss_pos = text.find("DISCUSS positions")
    other_pos = text.find("Other positions")
    # DISCUSS section comes before Other positions.
    assert 0 < discuss_pos < other_pos
    # DISCUSS body renders inline.
    assert "My DISCUSS body." in text
    # Tally appears in the header.
    assert "1 DISCUSS" in text
    assert "1 Yes" in text
    assert "1 No Objection" in text


def test_render_ballot_no_discuss_omits_that_section() -> None:
    ballot = _make_ballot(
        _pos("Alice Chen", "noobj"),
        _pos("Bob Smith", "yes"),
    )
    text = render_ballot(ballot)
    assert "DISCUSS positions" not in text
    assert "Other positions" in text


# --- File writing --------------------------------------------------------


def test_write_ballot_files_writes_per_draft_md(
    isolated_home: Path,
) -> None:
    cache = get_wg_file_cache_dir("wg")
    ballot = _make_ballot(_pos("Alice Chen", "discuss", discuss="x"))
    paths = write_ballot_files(cache, [ballot], verbose=Verbosity.QUIET)
    assert len(paths) == 1
    assert paths[0].endswith("ballots/draft-foo.md")
    content = Path(paths[0]).read_text()
    assert "draft-foo" in content


def test_write_ballot_files_wipes_stale(isolated_home: Path) -> None:
    # A stale ballot file from a previous gather (draft fell out of
    # window) must be removed; otherwise the consumer sees misleading
    # currently-active content.
    cache = get_wg_file_cache_dir("wg")
    from ietf_llm.paths import ballots_dir  # pylint: disable=import-outside-toplevel
    out_dir = ballots_dir(cache)
    import os  # pylint: disable=import-outside-toplevel
    os.makedirs(out_dir, exist_ok=True)
    stale = os.path.join(out_dir, "draft-stale.md")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("# stale\n")
    ballot = _make_ballot(_pos("Alice Chen", "noobj"))
    write_ballot_files(cache, [ballot], verbose=Verbosity.QUIET)
    # Stale file gone, new one present.
    assert not os.path.exists(stale)
    assert os.path.isfile(os.path.join(out_dir, "draft-foo.md"))


# --- Timeline events ----------------------------------------------------


def test_ballot_events_emits_one_event_per_in_window_position(
    isolated_home: Path,
) -> None:
    cache = get_wg_file_cache_dir("wg")
    now = datetime.now(timezone.utc)
    in_window = now - timedelta(days=5)
    pre_window = now - timedelta(days=400)
    ballot = Ballot(
        doc_name="draft-foo",
        positions=[
            _pos("Alice Chen", "noobj", when=in_window),
            _pos("Bob Smith", "discuss", when=pre_window),
        ],
        first_event=pre_window,
        latest_in_window=in_window,
        latest_rev="12",
    )
    cutoff = now - timedelta(days=365)
    events = ballot_events([ballot], cache, cutoff)
    # Only Alice's in-window event lands on the timeline; Bob's stale
    # DISCUSS is rendered in the ballot file but doesn't repeat on the
    # timeline.
    assert len(events) == 1
    assert "Alice Chen" in events[0].title
    assert events[0].kind == "ballot"
    # Link points at the per-draft ballot file (relpath within cache).
    assert events[0].link == "ballots/draft-foo.md"
