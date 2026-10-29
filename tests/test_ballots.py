"""Tests for IESG ballot gathering and rendering.

The Datatracker calls themselves are mocked at the `_get_json` seam
in `gather.sources.ballots`, so the tests exercise scoping, position
collapse (latest-event-per-AD wins), and rendering without hitting
the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ietf_llm.digest import timeline
from ietf_llm.digest.overview import build_overview
from ietf_llm.gather.sources import ballots as ballots_module
from ietf_llm.gather.sources.ballots import (
    Ballot,
    Position,
    ballot_events,
    fetch_ballots,
    render_ballot,
    write_ballot_files,
)
from ietf_llm.log import Verbosity
from ietf_llm.mcp.digest import tool_read_digest
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people import Registry


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
    ballot: Optional[int] = None,
) -> Dict[str, Any]:
    """Shape one ballotpositiondocevent JSON object. `ballot` is the id of the
    BallotDocEvent the position belongs to (absent when None)."""
    obj: Dict[str, Any] = {
        "doc": f"/api/v1/doc/document/{doc_name}/",
        "balloter": f"/api/v1/person/person/{person_id}/",
        "pos": f"/api/v1/name/ballotpositionname/{pos_slug}/",
        "time": when_iso,
        "rev": rev,
        "discuss": discuss,
        "comment": comment,
    }
    if ballot is not None:
        obj["ballot"] = f"/api/v1/doc/ballotdocevent/{ballot}/"
    return obj


def _roles(*person_ids: int) -> Dict[str, Any]:
    """A `group/role` response naming these people."""
    return {
        "objects": [{"person": f"/api/v1/person/person/{pid}/"} for pid in person_ids],
        "meta": {"next": None},
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
        "doc__name__in=draft-foo": {
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
        "doc__name__in=draft-foo": {
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


def test_cleared_position_is_not_a_position(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`norecord` is a balloter holding no position — either never posted or
    posted and later cleared. It is the real slug Datatracker emits (there is
    no `noupcoming`), so before this it fell through the skip and rendered as
    a `NORECORD` position, inflating the tally."""
    now = datetime.now(timezone.utc)
    early = (now - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    late = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {
            "objects": [_position_event("draft-foo", 101, "norecord", late)],
        },
        "doc__name__in=draft-foo": {
            "objects": [
                _position_event("draft-foo", 101, "discuss", early, discuss="Old."),
                _position_event("draft-foo", 101, "norecord", late),
                _position_event("draft-foo", 102, "yes", late),
            ],
        },
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/102/": {"name": "Bob Smith"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    positions = out[0].positions
    assert [p.name for p in positions] == ["Bob Smith"]
    text = render_ballot(out[0])
    assert "NORECORD" not in text and "No Record" not in text
    assert "**Tally:** 1 Yes" in text
    # The cleared DISCUSS must not come back either: the latest event wins,
    # and the latest event is that Alice holds nothing.
    assert "Old." not in text


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
        "doc__name__in=draft-foo": {
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


# --- Batched fetch (no N+1) ----------------------------------------------


def test_multiple_docs_fetched_in_one_batched_query(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two in-scope docs must be pulled with a single `doc__name__in` query
    # whose body carries events for both, not one request per doc.
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = _stub_get_json(monkeypatch, {
        "doc__group__acronym": {
            "objects": [
                _position_event("draft-foo", 101, "noobj", recent),
                _position_event("draft-bar", 202, "discuss", recent),
            ],
        },
        "doc__name__in=": {
            "objects": [
                _position_event("draft-foo", 101, "noobj", recent),
                _position_event("draft-bar", 202, "discuss", recent),
            ],
        },
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/202/": {"name": "Bob Smith"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert {b.doc_name for b in out} == {"draft-foo", "draft-bar"}
    # The batched history query was issued exactly once for both docs.
    history_calls = [c for c in calls if "doc__name__in=" in c]
    assert len(history_calls) == 1


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
        # From the module's own map, so a test can't agree with the code
        # about a label the API never emits.
        pos_label=ballots_module._POSITION_LABELS[slug],
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


def test_render_ballot_heads_the_section_by_the_position_present() -> None:
    # An IRSG Block holds publication the same way a DISCUSS does, so it
    # renders with its filed text — but under its own name, not "DISCUSS".
    ballot = _make_ballot(
        _pos("Alice Chen", "noobj"),
        _pos("Bob Smith", "block", discuss="Blocking on the security model."),
    )
    text = render_ballot(ballot)
    assert "## Block positions" in text
    assert "DISCUSS" not in text
    # The filed text survives; before, a Block fell into "Other positions",
    # which renders only the comment and drops the body entirely.
    assert "Blocking on the security model." in text
    assert "1 Block" in text


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


# --- One ballot per document ---------------------------------------------


def _iso(days_ago: int) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_only_the_newest_ballot_is_read(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A re-balloted document carries the old ballot's positions in the same
    # history. Bob's DISCUSS on ballot 1 is closed; Alice re-balloted on 2.
    history = [
        _position_event("draft-foo", 101, "discuss", _iso(300), discuss="Old.", ballot=1),
        _position_event("draft-foo", 102, "discuss", _iso(300), discuss="Closed.", ballot=1),
        _position_event("draft-foo", 101, "yes", _iso(5), ballot=2),
    ]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": [history[2]]},
        "doc__name__in=": {"objects": history},
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/102/": {"name": "Bob Smith"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out is not None
    assert [(p.name, p.pos_slug) for p in out[0].positions] == [("Alice Chen", "yes")]
    text = render_ballot(out[0])
    assert "Closed." not in text and "Bob Smith" not in text
    assert "**Tally:** 1 Yes" in text


def test_events_without_a_ballot_field_are_one_ballot(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _position_event("draft-foo", 101, "yes", _iso(5)),
        _position_event("draft-foo", 102, "noobj", _iso(4)),
    ]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": events},
        "doc__name__in=": {"objects": events},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out is not None and len(out[0].positions) == 2


def test_former_balloters_are_listed_but_not_counted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _position_event("draft-foo", 101, "yes", _iso(5), ballot=7),
        _position_event("draft-foo", 102, "discuss", _iso(5), discuss="Departed.", ballot=7),
    ]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": events},
        "doc__name__in=": {"objects": events},
        "group__acronym=iesg": _roles(101, 103),
        "group__acronym=irsg": _roles(),
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/102/": {"name": "Bob Smith"},
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out is not None
    by_name = {p.name: p for p in out[0].positions}
    assert by_name["Alice Chen"].current and not by_name["Bob Smith"].current
    text = render_ballot(out[0])
    assert "**Tally:** 1 Yes" in text
    assert "DISCUSS" not in text.split("## Former balloters")[0]
    assert "Bob Smith (former AD) — DISCUSS" in text
    # The departed AD's body is not presented as a live objection.
    assert "Departed." not in text


def test_irsg_members_count_as_current(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [_position_event("draft-irtf-foo", 201, "yes", _iso(5), ballot=3)]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": events},
        "doc__name__in=": {"objects": events},
        "group__acronym=iesg": _roles(101),
        "group__acronym=irsg": _roles(201),
        "/api/v1/person/person/201/": {"name": "Rae Search"},
    })
    out = fetch_ballots("rg", months=12, verbose=Verbosity.QUIET)
    assert out is not None and out[0].positions[0].current


@pytest.mark.parametrize("failing", ["group__acronym=iesg", "group__acronym=irsg"])
def test_unknown_current_balloters_means_no_filtering(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, failing: str,
) -> None:
    # If the role list can't be read, "nobody is current" would label every
    # balloter a former one. Better to show them all.
    events = [_position_event("draft-foo", 102, "yes", _iso(5), ballot=7)]
    responses: Dict[str, Optional[Dict[str, Any]]] = {
        "doc__group__acronym": {"objects": events},
        "doc__name__in=": {"objects": events},
        "group__acronym=iesg": _roles(101),
        "group__acronym=irsg": _roles(201),
    }
    responses[failing] = None
    _stub_get_json(monkeypatch, responses)
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out is not None and out[0].positions[0].current


def test_charter_ballot_is_headed_as_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # charter-ietf-dispatch has had four ballots; the three closed ones must
    # not be merged into the file.
    events = [
        _position_event("charter-ietf-wg", 101, "block", _iso(900), ballot=1),
        _position_event("charter-ietf-wg", 102, "block", _iso(600), ballot=2),
        _position_event("charter-ietf-wg", 103, "yes", _iso(300), ballot=3),
        _position_event("charter-ietf-wg", 101, "yes", _iso(5), ballot=4),
        _position_event("charter-ietf-wg", 102, "noobj", _iso(4), ballot=4),
    ]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": events[3:]},
        "doc__name__in=": {"objects": events},
        "group__acronym=iesg": _roles(101, 102, 103),
        "group__acronym=irsg": _roles(),
    })
    out = fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
    assert out is not None
    text = render_ballot(out[0])
    assert text.startswith("# IESG charter ballot: `charter-ietf-wg`")
    assert "**Tally:** 1 Yes, 1 No Objection" in text
    assert "Block" not in text


# --- A failed fetch is not "no ballots" ----------------------------------


def test_failed_scoping_fetch_is_none_not_empty(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {"doc__group__acronym": None})
    assert fetch_ballots("wg", months=12, verbose=Verbosity.QUIET) is None


def test_failed_history_fetch_is_none(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [_position_event("draft-foo", 101, "yes", _iso(5), ballot=1)]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": events},
        "doc__name__in=": None,
    })
    assert fetch_ballots("wg", months=12, verbose=Verbosity.QUIET) is None


def test_no_ballot_activity_is_still_an_empty_list(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, {"doc__group__acronym": {"objects": []}})
    assert fetch_ballots("wg", months=12, verbose=Verbosity.QUIET) == []


def _clock(hour: int) -> Any:
    """A `datetime` class whose now() is fixed at `hour` o'clock on a set day."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime(2026, 6, 15, hour, 7, 31, tzinfo=tz)

    return _Frozen


def test_scoping_url_is_stable_within_a_day(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cutoff is part of the URL the ETag cache is keyed on.
    urls = []
    for hour in (3, 17):
        monkeypatch.setattr(ballots_module, "datetime", _clock(hour))
        calls = _stub_get_json(monkeypatch, {"doc__group__acronym": {"objects": []}})
        fetch_ballots("wg", months=12, verbose=Verbosity.QUIET)
        urls.append(calls[0])
    assert urls[0] == urls[1]
    assert "time__gte=2025-06-20T00:00:00" in urls[0]


def test_failed_fetch_leaves_the_ballot_files_alone(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = get_wg_file_cache_dir("wg")
    ballot_file = Path(cache) / "ballots" / "draft-foo.md"
    ballot_file.parent.mkdir(parents=True)
    ballot_file.write_text("# last good gather\n", encoding="utf-8")
    _stub_get_json(monkeypatch, {"doc__group__acronym": None})
    timeline.build_events("wg", cache, Registry(), verbose=Verbosity.QUIET)
    assert ballot_file.read_text(encoding="utf-8") == "# last good gather\n"

    # Whereas a successful "no activity" really does clear it.
    _stub_get_json(monkeypatch, {"doc__group__acronym": {"objects": []}})
    timeline.build_events("wg", cache, Registry(), verbose=Verbosity.QUIET)
    assert not ballot_file.exists()


# --- Writer -> reader ------------------------------------------------------


def test_written_ballot_reads_back_with_the_right_tally(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real gather path over a re-balloted document with a departed
    AD, then read the output the way a consumer does: the overview's DISCUSS
    flag (which parses the written `**Tally:**` line) and
    `read_digest(event_kind="ballot")`."""
    cache = get_wg_file_cache_dir("wg")
    history = [
        # Closed first ballot: a standing-looking DISCUSS from Dana.
        _position_event("draft-ietf-wg-foo", 104, "discuss", _iso(400), discuss="Closed.", ballot=1),
        _position_event("draft-ietf-wg-foo", 101, "noobj", _iso(400), ballot=1),
        # Current ballot: Alice DISCUSS, Bob yes, Carol (departed) DISCUSS.
        _position_event("draft-ietf-wg-foo", 101, "discuss", _iso(6), discuss="Live.", ballot=2),
        _position_event("draft-ietf-wg-foo", 102, "yes", _iso(5), ballot=2),
        _position_event("draft-ietf-wg-foo", 103, "discuss", _iso(5), discuss="Gone.", ballot=2),
    ]
    _stub_get_json(monkeypatch, {
        "doc__group__acronym": {"objects": history[2:]},
        "doc__name__in=": {"objects": history},
        "group__acronym=iesg": _roles(101, 102, 104),
        "group__acronym=irsg": _roles(),
        "/api/v1/person/person/101/": {"name": "Alice Chen"},
        "/api/v1/person/person/102/": {"name": "Bob Smith"},
        "/api/v1/person/person/103/": {"name": "Carol Lee"},
        "/api/v1/person/person/104/": {"name": "Dana Fox"},
    })
    timeline.write_timeline_digest("wg", cache, Registry(), verbose=Verbosity.QUIET)

    body = (Path(cache) / "ballots" / "draft-ietf-wg-foo.md").read_text(encoding="utf-8")
    assert "**Tally:** 1 DISCUSS, 1 Yes" in body
    assert "Live." in body and "Closed." not in body and "Gone." not in body

    overview = build_overview("wg", cache)
    assert "Blocked on IESG DISCUSS (1)" in overview
    assert "draft-ietf-wg-foo" in overview

    rows = tool_read_digest("wg", "timeline", event_kind="ballot")
    assert "Alice Chen → DISCUSS" in rows
    assert "Bob Smith → Yes" in rows
    assert "Dana Fox" not in rows  # her only position was on the closed ballot
