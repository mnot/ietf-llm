"""Tests for discovering which mailing lists a --author person is on.

Datatracker is stubbed at `_get_json`, the acronym→archive-name step at
`get_mailing_list_name`, and the mailarchive probe at
`validate_list_names`, so these cover candidate ordering, the
`list_email` filter, the always-on lists, dedupe, and the cap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import author_lists
from ietf_llm.gather.sources.author_lists import (
    ALWAYS_LISTS,
    MAX_DISCOVERED,
    discover_author_lists,
)
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    roles: List[str],
    doc_groups: Dict[str, str],
    groups: List[Dict[str, Any]],
) -> List[str]:
    """Stub the Datatracker seams. Returns the validated-names sink."""
    validated: List[str] = []

    def fake_get_json(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        if "/group/role/" in url:
            return {
                "objects": [
                    {"group": f"/api/v1/group/group/{gid}/"} for gid in roles
                ]
            }
        if "/doc/document/" in url:
            return {
                "objects": [
                    {"name": name, "group": f"/api/v1/group/group/{gid}/"}
                    for name, gid in doc_groups.items()
                ]
            }
        if "/group/group/" in url:
            return {"objects": groups}
        return None

    monkeypatch.setattr(author_lists, "_get_json", fake_get_json)
    # Archive name == acronym unless a test says otherwise.
    monkeypatch.setattr(author_lists, "get_mailing_list_name", lambda acr: acr)

    def fake_validate(
        names: List[str], verbose: Any = None, source: str = ""
    ) -> List[str]:
        validated.extend(names)
        return list(names)

    monkeypatch.setattr(author_lists, "validate_list_names", fake_validate)
    return validated


def _group(
    gid: int, acronym: str, list_email: Optional[str] = None
) -> Dict[str, Any]:
    """A group object. `list_email` defaults to the usual `<acronym>@ietf.org`;
    pass an off-IETF address to exercise the mirror-name lookup."""
    if list_email is None:
        list_email = f"{acronym}@ietf.org"
    return {"id": gid, "acronym": acronym, "list_email": list_email}


def test_always_lists_come_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, roles=["1"], doc_groups={}, groups=[_group(1, "httpbis")])
    result = discover_author_lists(103881, [], Q)
    assert result[: len(ALWAYS_LISTS)] == list(ALWAYS_LISTS)
    assert "httpbis" in result


def test_always_lists_present_with_no_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Someone with no roles and no drafts still gets the Last Call
    lists — that is where cross-area review lives."""
    _stub(monkeypatch, roles=[], doc_groups={}, groups=[])
    assert discover_author_lists(1, [], Q) == list(ALWAYS_LISTS)


def test_groups_without_a_list_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An area or IAB ASG exists on Datatracker but has no list of its
    own; an empty `list_email` is what filters those out."""
    _stub(
        monkeypatch,
        roles=["1", "2"],
        doc_groups={},
        groups=[_group(1, "app", list_email=""), _group(2, "tls")],
    )
    result = discover_author_lists(1, [], Q)
    assert "tls" in result
    assert "app" not in result


def test_roles_rank_ahead_of_draft_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role is the stronger claim, and first-seen order is what the
    cap truncates against."""
    _stub(
        monkeypatch,
        roles=["2"],
        doc_groups={"draft-x": "1"},
        groups=[_group(1, "fromdraft"), _group(2, "fromrole")],
    )
    result = discover_author_lists(1, ["draft-x"], Q)
    assert result.index("fromrole") < result.index("fromdraft")


def test_draft_groups_are_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WG they wrote in but hold no role in still shows up."""
    _stub(
        monkeypatch,
        roles=[],
        doc_groups={"draft-ietf-dnsop-x": "5"},
        groups=[_group(5, "dnsop")],
    )
    assert "dnsop" in discover_author_lists(1, ["draft-ietf-dnsop-x"], Q)


def test_duplicate_groups_appear_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(
        monkeypatch,
        roles=["1", "1"],
        doc_groups={"draft-x": "1"},
        groups=[_group(1, "httpbis")],
    )
    result = discover_author_lists(1, ["draft-x"], Q)
    assert result.count("httpbis") == 1


def test_group_list_matching_an_always_list_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, roles=["1"], doc_groups={}, groups=[_group(1, "ietf")])
    result = discover_author_lists(1, [], Q)
    assert result.count("ietf") == 1


def test_off_ietf_list_resolves_to_the_mirror_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WG hosted off IETF infrastructure (httpbis runs at w3.org) is
    mirrored under a different name, and only the group's Additional
    Resources say which — so it pays for the extra lookup."""
    _stub(
        monkeypatch,
        roles=["1"],
        doc_groups={},
        groups=[_group(1, "httpbis", list_email="ietf-http-wg@w3.org")],
    )
    monkeypatch.setattr(
        author_lists,
        "get_mailing_list_name",
        lambda acr: "httpbisa" if acr == "httpbis" else acr,
    )
    result = discover_author_lists(1, [], Q)
    assert "httpbisa" in result
    assert "ietf-http-wg" not in result


def test_ietf_hosted_list_needs_no_extra_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fetch_groups` already returned `list_email`, so the common case
    must not spend another Datatracker request per candidate group."""
    _stub(monkeypatch, roles=["1"], doc_groups={}, groups=[_group(1, "dnsop")])

    def boom(acronym: str) -> str:
        raise AssertionError(f"redundant group lookup for {acronym!r}")

    monkeypatch.setattr(author_lists, "get_mailing_list_name", boom)
    assert "dnsop" in discover_author_lists(1, [], Q)


def test_cap_truncates_and_keeps_always_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    over = MAX_DISCOVERED + 5
    _stub(
        monkeypatch,
        roles=[str(i) for i in range(over)],
        doc_groups={},
        groups=[_group(i, f"wg{i:03d}") for i in range(over)],
    )
    result = discover_author_lists(1, [], Q)
    assert result[: len(ALWAYS_LISTS)] == list(ALWAYS_LISTS)
    assert len(result) == len(ALWAYS_LISTS) + MAX_DISCOVERED


def test_validation_is_labelled_author_not_the_cli_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These names were discovered, not typed — reporting them as
    `--mailing-list` would blame the user for a list they never named."""
    seen: Dict[str, str] = {}

    _stub(monkeypatch, roles=[], doc_groups={}, groups=[])

    def fake_validate(
        names: List[str], verbose: Any = None, source: str = ""
    ) -> List[str]:
        seen["source"] = source
        return list(names)

    monkeypatch.setattr(author_lists, "validate_list_names", fake_validate)
    discover_author_lists(1, [], Q)
    assert seen["source"] == "--author"


def test_unarchived_lists_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group's `list_email` can name a list mailarchive doesn't carry
    (a review team inheriting its WG's off-IETF address)."""
    _stub(
        monkeypatch,
        roles=["1", "2"],
        doc_groups={},
        groups=[_group(1, "real"), _group(2, "notarchived")],
    )
    monkeypatch.setattr(
        author_lists,
        "validate_list_names",
        lambda names, verbose=None, source="": [
            n for n in names if n != "notarchived"
        ],
    )
    result = discover_author_lists(1, [], Q)
    assert "real" in result
    assert "notarchived" not in result
