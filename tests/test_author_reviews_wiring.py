"""Sequencer wiring for the `--author` passes.

`sources.reviews`, `sources.author_lists` and `sources.author_mail` are
unit-tested separately; this covers how the sequencer drives them —
that one person resolution feeds all three, that an unresolved spec
fires none of them, and the two behaviours documented only here: mail
skips a list the user named explicitly (it is gathered whole instead),
and the chosen set is persisted.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ietf_llm.corpus.identity import _source_subject
from ietf_llm.gather import sequencer
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


def _args(**kw: Any) -> argparse.Namespace:
    base: Dict[str, Any] = {
        "wg": "mnot",
        "author": None,
        "new_drafts": False,
        "draft": None,
        "mailing_list": None,
        "months": 12,
    }
    base.update(kw)
    return argparse.Namespace(**base)


# --- author resolution ----------------------------------------------------


def _patch_drafts(
    monkeypatch: pytest.MonkeyPatch, *, resolved: Optional[Tuple[int, str]]
) -> List[Dict[str, Any]]:
    """Stub the draft seams; returns the recorded process_extra_drafts calls."""
    calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        sequencer, "resolve_person", lambda spec, verbose=None: resolved
    )
    monkeypatch.setattr(
        sequencer, "fetch_author_draft_names", lambda pid, verbose=None: ["draft-x-00"]
    )

    def fake_extra(
        names: List[str],
        cache: str,
        verbose: Any = None,
        latest_only: bool = False,
    ) -> None:
        calls.append({"names": list(names), "latest_only": latest_only})

    monkeypatch.setattr(sequencer, "process_extra_drafts", fake_extra)
    monkeypatch.setattr(sequencer, "_persist_author_name", lambda wg, name: None)
    return calls


def test_resolved_author_is_returned_for_the_later_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One resolution feeds reviews and mail — re-resolving would cost
    another Datatracker round-trip, and list discovery needs the drafts."""
    _patch_drafts(monkeypatch, resolved=(103881, "Mark Nottingham"))
    result = sequencer._gather_dynamic_drafts(
        _args(author="mnot@mnot.net"), "/cache/mnot", {}, Q
    )
    assert result == (103881, "Mark Nottingham", ["draft-x-00"])


def test_unresolved_author_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_drafts(monkeypatch, resolved=None)
    assert (
        sequencer._gather_dynamic_drafts(
            _args(author="nobody@example.com"), "/c", {}, Q
        )
        is None
    )


def test_no_author_flag_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_drafts(monkeypatch, resolved=(1, "Someone"))
    assert sequencer._gather_dynamic_drafts(_args(), "/c", {}, Q) is None


def test_author_drafts_fetch_current_revision_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--author` names every draft one person ever wrote, and the
    revision stack dominated the gather to fetch history the index
    largely refuses to embed."""
    calls = _patch_drafts(monkeypatch, resolved=(103881, "Mark Nottingham"))
    sequencer._gather_dynamic_drafts(
        _args(author="mnot@mnot.net"), "/cache/mnot", {}, Q
    )
    assert calls == [{"names": ["draft-x-00"], "latest_only": True}]


# --- author mail ----------------------------------------------------------


def _patch_mail(
    monkeypatch: pytest.MonkeyPatch,
    *,
    addresses: List[str],
    discovered: List[str],
) -> List[Tuple[str, List[str], List[str]]]:
    """Stub the mail seams; returns the recorded sync calls."""
    calls: List[Tuple[str, List[str], List[str]]] = []

    monkeypatch.setattr(
        sequencer, "fetch_person_emails", lambda pid, verbose=None: addresses
    )
    monkeypatch.setattr(
        sequencer,
        "discover_author_lists",
        lambda pid, drafts, verbose=None: list(discovered),
    )
    monkeypatch.setattr(sequencer, "_persist_author_lists", lambda wg, lists: None)

    def fake_sync(
        wg: str,
        lists: List[str],
        addrs: List[str],
        months: Any = None,
        verbose: Any = None,
        note_fn: Any = None,
    ) -> int:
        calls.append((wg, list(lists), list(addrs)))
        return 0

    monkeypatch.setattr(sequencer, "sync_author_mail", fake_sync)
    return calls


_AUTHOR = (103881, "Mark Nottingham", ["draft-x-00"])


def test_mail_syncs_the_discovered_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mail(
        monkeypatch, addresses=["a@example.com"], discovered=["last-call", "dnsop"]
    )
    sequencer._gather_author_mail(_args(author="x"), _AUTHOR, Q)
    assert calls == [("mnot", ["last-call", "dnsop"], ["a@example.com"])]


def test_explicitly_named_lists_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--mailing-list dnsop` gathers that list in full, which is a
    superset of the sender-scoped pull — searching it again is waste."""
    calls = _patch_mail(
        monkeypatch, addresses=["a@example.com"], discovered=["last-call", "dnsop"]
    )
    sequencer._gather_author_mail(
        _args(author="x", mailing_list=["dnsop@ietf.org"]), _AUTHOR, Q
    )
    assert calls[0][1] == ["last-call"]


def test_persists_the_lists_actually_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--list` shows what a re-run will cover, so it must be the
    post-exclusion set, not the raw discovery."""
    _patch_mail(
        monkeypatch, addresses=["a@example.com"], discovered=["last-call", "dnsop"]
    )
    saved: Dict[str, List[str]] = {}
    monkeypatch.setattr(
        sequencer,
        "_persist_author_lists",
        lambda wg, lists: saved.__setitem__(wg, lists),
    )
    sequencer._gather_author_mail(
        _args(author="x", mailing_list=["dnsop"]), _AUTHOR, Q
    )
    assert saved == {"mnot": ["last-call"]}


def test_no_author_means_no_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_mail(monkeypatch, addresses=["a@example.com"], discovered=["x"])
    sequencer._gather_author_mail(_args(), None, Q)
    assert calls == []


def test_no_addresses_means_no_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an address to search FROM there is nothing to find, so
    don't spend the discovery requests either."""
    calls = _patch_mail(monkeypatch, addresses=[], discovered=["x"])

    def boom(pid: int, drafts: Any, verbose: Any = None) -> List[str]:
        raise AssertionError("discovery ran with no addresses to search")

    monkeypatch.setattr(sequencer, "discover_author_lists", boom)
    sequencer._gather_author_mail(_args(author="x"), _AUTHOR, Q)
    assert calls == []


# --- corpus description ---------------------------------------------------


def test_source_subject_reports_followed_lists() -> None:
    """Without this the subject line reads as a drafts-only corpus."""
    subject = _source_subject(
        {
            "author": "mnot@mnot.net",
            "author_name": "Mark Nottingham",
            "author_lists": ["last-call", "ietf", "dnsop"],
        }
    )
    assert "author: Mark Nottingham" in subject
    assert "mail on 3 list(s)" in subject


def test_source_subject_omits_lists_when_none_searched() -> None:
    subject = _source_subject({"author": "mnot@mnot.net"})
    assert "author: mnot@mnot.net" in subject
    assert "mail on" not in subject
