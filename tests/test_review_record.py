"""Tests for `review_record` — the review/ballot join keyed by revision.

The canned bodies are the shapes Datatracker actually served for
`draft-ietf-httpbis-no-vary-search` on 2026-08-13, trimmed to the fields the
reader uses: three completed reviews (two against -06, one against -07), a
rejected assignment and an assigned-but-never-completed one, and five ballot
positions of which one is against the current -08. That mix is the point —
every branch this renders exists because the real record has these rows.

Network is stubbed at the one seam (`live_lookup.cache._fetch_json`), as in
`test_live_lookup`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from ietf_llm import live_lookup
from ietf_llm.mcp.drafts import tool_review_record

# `time` is the document's last-modified stamp, which advances on later state
# changes; `_REVISIONS` carries when -08 was actually posted. They differ here
# on purpose — that gap is what made the header contradict its own table.
_DOC = {"name": "draft-ietf-httpbis-no-vary-search", "rev": "08", "time": "2026-08-12"}

_REVISIONS = {
    "meta": {"next": None},
    "objects": [{"rev": "08", "time": "2026-08-09T14:22:45Z"}],
}

_ASSIGNMENTS = {
    "meta": {"next": None},
    "objects": [
        {
            "assigned_on": "2026-07-07T22:21:57Z",
            "completed_on": "2026-07-27T19:30:54Z",
            "result": "/api/v1/name/reviewresultname/ready-issues/",
            "review_request": "/api/v1/review/reviewrequest/24520/",
            "reviewed_rev": "06",
            "reviewer": "/api/v1/person/email/mallory@example.net/",
            "state": "/api/v1/name/reviewassignmentstatename/completed/",
        },
        {
            "assigned_on": "2026-07-09T17:24:10Z",
            "completed_on": "2026-07-22T17:51:56Z",
            "result": "/api/v1/name/reviewresultname/issues/",
            "review_request": "/api/v1/review/reviewrequest/24530/",
            "reviewed_rev": "06",
            "reviewer": "/api/v1/person/email/corey@example.com/",
            "state": "/api/v1/name/reviewassignmentstatename/completed/",
        },
        {
            "assigned_on": "2026-07-08T18:51:24Z",
            "completed_on": "2026-08-07T14:48:32Z",
            "result": None,
            "review_request": "/api/v1/review/reviewrequest/24524/",
            # A rejected assignment carries an empty rev, not a missing key.
            "reviewed_rev": "",
            "reviewer": "/api/v1/person/email/julian@example.de/",
            "state": "/api/v1/name/reviewassignmentstatename/rejected/",
        },
        {
            "assigned_on": "2026-08-12T21:22:47Z",
            "completed_on": None,
            "result": None,
            "review_request": "/api/v1/review/reviewrequest/24524/",
            "reviewed_rev": "",
            "reviewer": "/api/v1/person/email/arnt@example.no/",
            "state": "/api/v1/name/reviewassignmentstatename/assigned/",
        },
        {
            "assigned_on": "2026-08-11T09:33:07Z",
            "completed_on": "2026-08-11T13:32:54Z",
            "result": "/api/v1/name/reviewresultname/ready/",
            "review_request": "/api/v1/review/reviewrequest/24758/",
            "reviewed_rev": "07",
            "reviewer": "/api/v1/person/email/corey@example.com/",
            "state": "/api/v1/name/reviewassignmentstatename/completed/",
        },
    ],
}

_REQUESTS = {
    "meta": {"next": None},
    "objects": [
        {
            "id": 24520,
            "team": "/api/v1/group/group/1730/",
            "type": "/api/v1/name/reviewtypename/lc/",
        },
        {
            "id": 24524,
            "team": "/api/v1/group/group/2196/",
            "type": "/api/v1/name/reviewtypename/lc/",
        },
        {
            "id": 24530,
            "team": "/api/v1/group/group/1261/",
            "type": "/api/v1/name/reviewtypename/lc/",
        },
        {
            "id": 24758,
            "team": "/api/v1/group/group/1261/",
            "type": "/api/v1/name/reviewtypename/telechat/",
        },
    ],
}

_GROUPS = {
    "meta": {"next": None},
    "objects": [
        {"id": 1730, "acronym": "genart"},
        {"id": 2196, "acronym": "artart"},
        {"id": 1261, "acronym": "secdir"},
    ],
}

_RESULTS = {
    "meta": {"next": None},
    "objects": [
        {"slug": "ready-issues", "name": "Ready with Issues"},
        {"slug": "issues", "name": "Has Issues"},
        {"slug": "ready", "name": "Ready"},
    ],
}

_EMAILS = {
    "meta": {"next": None},
    "objects": [
        {"address": "mallory@example.net", "person": "/api/v1/person/person/1/"},
        {"address": "corey@example.com", "person": "/api/v1/person/person/2/"},
        {"address": "julian@example.de", "person": "/api/v1/person/person/3/"},
        {"address": "arnt@example.no", "person": "/api/v1/person/person/4/"},
    ],
}

_PEOPLE = {
    "meta": {"next": None},
    "objects": [
        {"id": 1, "name": "Mallory Knodel"},
        {"id": 2, "name": "Corey Bonnell"},
        {"id": 3, "name": "Julian Reschke"},
        {"id": 4, "name": "Arnt Gulbrandsen"},
        {"id": 10, "name": "Mike Bishop"},
        {"id": 11, "name": "Gorry Fairhurst"},
        {"id": 12, "name": "Deb Cooley"},
    ],
}


def _position(person: int, pos: str, rev: str, when: str, **extra: Any) -> Dict[str, Any]:
    return {
        "balloter": f"/api/v1/person/person/{person}/",
        "pos": f"/api/v1/name/ballotpositionname/{pos}/",
        "rev": rev,
        "time": when,
        **extra,
    }


_POSITIONS = {
    "meta": {"next": None},
    "objects": [
        _position(10, "yes", "07", "2026-08-03T10:00:00Z"),
        _position(11, "noobj", "07", "2026-08-10T10:00:00Z"),
        _position(12, "noobj", "08", "2026-08-12T23:20:34Z"),
    ],
}


def _canned(url: str) -> Optional[Dict[str, Any]]:
    if "/doc/document/draft-ietf-httpbis-no-vary-search/" in url:
        return _DOC
    if "/doc/document/" in url:
        return None  # unknown document
    if "/doc/newrevisiondocevent/" in url:
        return _REVISIONS
    if "/review/reviewassignment/" in url:
        return _ASSIGNMENTS
    if "/review/reviewrequest/" in url:
        return _REQUESTS
    if "/group/group/" in url:
        return _GROUPS
    if "/name/reviewresultname/" in url:
        return _RESULTS
    if "/person/email/" in url:
        return _EMAILS
    if "/person/person/" in url:
        return _PEOPLE
    if "/ballotpositiondocevent/" in url:
        return _POSITIONS
    return None


@pytest.fixture(name="urls")
def _stub_fetch(monkeypatch, isolated_home):
    """Stub the network seam, returning the list of URLs each test requested."""
    seen: List[str] = []

    def _fetch(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        seen.append(url)
        return _canned(url)

    live_lookup._reset_cache()  # pylint: disable=protected-access
    monkeypatch.setattr(live_lookup.cache, "_fetch_json", _fetch)
    yield seen
    live_lookup._reset_cache()  # pylint: disable=protected-access


def _record() -> str:
    return tool_review_record("draft-ietf-httpbis-no-vary-search")


# --- the join ---------------------------------------------------------------


def test_assignments_are_filtered_by_document(urls) -> None:
    """`?doc=` is not an error on this endpoint — it is silently ignored and
    returns every assignment Datatracker holds, which reads as a plausible
    result. Only `review_request__doc__name` actually filters."""
    _record()
    assignment_urls = [url for url in urls if "/review/reviewassignment/" in url]
    assert assignment_urls
    for url in assignment_urls:
        assert "review_request__doc__name=draft-ietf-httpbis-no-vary-search" in url
        assert "?doc=" not in url


def test_each_review_carries_the_revision_it_was_cast_against(urls) -> None:
    out = _record()
    assert "| -06 | genart | LC | Mallory Knodel | Ready with Issues | 2026-07-27 |" in out
    assert "| -06 | secdir | LC | Corey Bonnell | Has Issues | 2026-07-22 |" in out
    assert "| -07 | secdir | TELECHAT | Corey Bonnell | Ready | 2026-08-11 |" in out


def test_unproductive_assignments_are_kept(urls) -> None:
    # A directorate that returned nothing is a fact about the coverage; both
    # rows vanish if the reader filters to completed assignments.
    out = _record()
    assert "Julian Reschke | _rejected_" in out
    assert "Arnt Gulbrandsen | _assigned_" in out
    assert "2 assignments produced no review" in out


def test_positions_carry_their_revision(urls) -> None:
    out = _record()
    assert "| -07 | Yes | Mike Bishop | 2026-08-03 |" in out
    assert "| -08 | No Objection | Deb Cooley | 2026-08-12 |" in out


# --- the verdict ------------------------------------------------------------


def test_verdict_splits_reviews_from_ballot(urls) -> None:
    """The halves disagree here — the reviews are one and two revisions back
    while one AD has balloted on the current text. A single "newest input"
    would report this document as reviewed."""
    out = _record()
    assert "**Reviews:** 1 against -07 (1 revision behind), 2 against -06" in out
    assert "**Ballot:** 1 against -08, 4 against -07" not in out  # only 3 positions
    assert "**Ballot:** 1 against -08, 2 against -07 (1 revision behind)." in out
    # Nothing in the review half reached -08, so no all-clear is implied.
    assert "against -08 — the current text" not in out


def test_warns_when_nothing_examined_the_current_revision(monkeypatch, urls) -> None:
    # Same record, but the -08 position moved back to -07: now no review and
    # no position has seen the current text, which is the headline.
    monkeypatch.setitem(_POSITIONS["objects"][2], "rev", "07")
    try:
        out = _record()
        assert "**Nothing in this record has examined -08.**" in out
        assert "seen by no reviewer and no balloter" in out
    finally:
        _POSITIONS["objects"][2]["rev"] = "08"


def test_no_warning_when_the_current_revision_was_reviewed(urls) -> None:
    # A position against -08 exists, so the record has been examined at the
    # current revision and the warning must not fire.
    assert "Nothing in this record has examined" not in _record()


# --- collapsing and omissions -----------------------------------------------


def test_latest_position_per_balloter_wins(monkeypatch, urls) -> None:
    """The endpoint returns one row per position *change*, so a balloter who
    revised a DISCUSS to No Objection would otherwise appear in both."""
    revised = {
        "meta": {"next": None},
        "objects": [
            _position(
                10, "discuss", "06", "2026-07-01T10:00:00Z", discuss="Old concern."
            ),
            *_POSITIONS["objects"],
        ],
    }
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            revised if "/ballotpositiondocevent/" in url else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access
    out = _record()
    assert "| -07 | Yes | Mike Bishop | 2026-08-03 |" in out
    assert "DISCUSS" not in out
    assert "3 positions" in out


def test_unposted_positions_are_dropped(monkeypatch, urls) -> None:
    # `norecord` is what Datatracker records for a balloter holding no
    # position — never posted, or posted and later cleared.
    empty = {
        "meta": {"next": None},
        "objects": [_position(11, "norecord", "", "2026-08-01T10:00:00Z")],
    }
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            empty if "/ballotpositiondocevent/" in url else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access
    out = _record()
    assert "**Ballot:** not opened." in out
    assert "Gorry Fairhurst" not in out


def test_standing_discuss_points_at_the_gathered_ballot_file(
    monkeypatch, urls
) -> None:
    # The DISCUSS body is long and already gathered per draft; this tool
    # carries the shape of the ballot and sends the reader there for the text.
    held = {
        "meta": {"next": None},
        "objects": [
            _position(11, "discuss", "08", "2026-08-12T10:00:00Z", discuss="Blocking."),
        ],
    }
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            held if "/ballotpositiondocevent/" in url else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access
    out = _record()
    assert "| -08 | **DISCUSS** | Gorry Fairhurst | 2026-08-12 |" in out
    assert "ballots/draft-ietf-httpbis-no-vary-search.md" in out


def _stub_positions(monkeypatch, objects: List[Dict[str, Any]]) -> None:
    body = {"meta": {"next": None}, "objects": objects}
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            body if "/ballotpositiondocevent/" in url else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access


def test_cleared_position_does_not_stand_in_for_an_examination(
    monkeypatch, urls
) -> None:
    """`norecord` is a balloter holding *no* position. Counted as one, it puts
    its revision into the examined set and silently withholds the headline —
    an AD who cleared their position standing in for one who read the text."""
    _stub_positions(
        monkeypatch,
        [
            _position(10, "yes", "06", "2026-08-03T10:00:00Z"),
            _position(11, "norecord", "08", "2026-08-12T10:00:00Z"),
        ],
    )
    out = _record()
    assert "**Nothing in this record has examined -08.**" in out
    assert "NORECORD" not in out
    assert "No Record" not in out


def test_recuse_is_shown_but_is_not_an_examination(monkeypatch, urls) -> None:
    # A Recuse is a declaration of not participating. It belongs in the table
    # — the ballot's shape includes it — but not in the examined set.
    _stub_positions(
        monkeypatch,
        [
            _position(10, "yes", "06", "2026-08-03T10:00:00Z"),
            _position(11, "recuse", "08", "2026-08-12T10:00:00Z"),
        ],
    )
    out = _record()
    assert "| -08 | Recuse | Gorry Fairhurst | 2026-08-12 |" in out
    assert "**Nothing in this record has examined -08.**" in out
    assert "**Ballot:** 1 against -06" in out


def test_iesg_position_vocabulary_matches_datatracker(urls) -> None:
    # The full `/api/v1/name/ballotpositionname/` set, including the four the
    # IAB / IRSG ballots use. A slug missing from the map leaks in upper case
    # through the fallback, so the guard is that none of them do.
    for slug in ("block", "concern", "moretime", "notready", "abstain"):
        assert slug in live_lookup.reviews._POSITION_LABELS  # pylint: disable=protected-access
        assert live_lookup.reviews._POSITION_LABELS[slug] != slug.upper()  # pylint: disable=protected-access


# --- what the record does not know ------------------------------------------


def test_a_failed_fetch_is_not_rendered_as_an_empty_record(monkeypatch, urls) -> None:
    """A Datatracker outage on the list endpoints must not read as a draft
    nobody has reviewed. The doc fetch still answers (it can come off the disk
    cache), so without the flag this renders a confident, wrong absence."""
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            None
            if ("/review/reviewassignment/" in url or "/ballotpositiondocevent/" in url)
            else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access
    out = _record()
    assert "Datatracker did not answer for the reviews and ballot" in out
    assert "this record is incomplete" in out
    # None of the absence claims may appear.
    assert "none requested" not in out
    assert "not opened" not in out
    assert "Nothing in this record has examined" not in out


def test_never_reviewed_draft_gets_no_gap_headline(monkeypatch, urls) -> None:
    """With nothing ever cast there is no "since" and no earlier revision to
    diff; the headline would read as "reviewed, then moved on"."""
    monkeypatch.setattr(
        live_lookup.cache,
        "_fetch_json",
        lambda url, timeout=10.0: (
            {"meta": {"next": None}, "objects": []}
            if ("/review/reviewassignment/" in url or "/ballotpositiondocevent/" in url)
            else _canned(url)
        ),
    )
    live_lookup._reset_cache()  # pylint: disable=protected-access
    out = _record()
    assert "Nothing in this record has examined" not in out
    assert "- **Reviews:** none requested." in out
    assert "- **Ballot:** not opened." in out


def test_revision_is_dated_from_its_own_posting_event(urls) -> None:
    # Not from the document's `time`, which advances on later state changes
    # and would date -08 after positions that were cast against it.
    assert "**Current revision:** -08 (2026-08-09)" in _record()


def test_query_values_are_escaped(monkeypatch, urls) -> None:
    """`+` is a space in a query string, and 297 Datatracker person emails
    contain one — unescaped, the name never comes back."""
    plussed = {
        "meta": {"next": None},
        "objects": [
            dict(
                _ASSIGNMENTS["objects"][0],
                reviewer="/api/v1/person/email/lars+ietf@example.net/",
            )
        ],
    }
    def _fetch(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        urls.append(url)  # this stub replaces the fixture's, so it records too
        return plussed if "/review/reviewassignment/" in url else _canned(url)

    monkeypatch.setattr(live_lookup.cache, "_fetch_json", _fetch)
    live_lookup._reset_cache()  # pylint: disable=protected-access
    _record()
    email_urls = [url for url in urls if "/person/email/?" in url]
    assert email_urls
    assert any("lars%2Bietf%40example.net" in url for url in email_urls)


def test_unknown_document_is_reported(urls) -> None:
    out = tool_review_record("draft-ietf-httpbis-nope")
    assert "no document named" in out
    assert "draft-ietf-httpbis-nope" in out


def test_empty_name_is_refused(urls) -> None:
    assert "Provide a draft name" in tool_review_record("  ")
