"""Tests for sender-scoped mail: one person's messages, quotes intact.

The IMAP server is replaced with a fake that records the SEARCH criteria
it was handed, so these cover the search terms themselves (FROM per
address, SINCE window) and the fact that nothing beyond the sender's own
messages is pulled — the context comes from the quotes those messages
carry, not from fetching the thread around them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from ietf_llm.gather.sources import author_mail
from ietf_llm.gather.sources.author_mail import sync_author_mail
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


class _FakeIMAP:
    """Minimal IMAP4_SSL stand-in.

    `search_results` maps a substring of the SEARCH criteria to the UIDs
    returned for it.
    """

    def __init__(
        self,
        search_results: Dict[str, List[int]],
        select_ok: bool = True,
    ) -> None:
        self.search_results = search_results
        self.select_ok = select_ok
        self.criteria: List[str] = []
        self.fetched: List[str] = []

    def login(self, user: str, password: str) -> None:
        return None

    def select(self, folder: str, readonly: bool = False) -> Tuple[str, Any]:
        return ("OK" if self.select_ok else "NO", None)

    def logout(self) -> None:
        return None

    def uid(self, command: str, *args: str) -> Tuple[str, Any]:
        if command == "search":
            criteria = args[0]
            self.criteria.append(criteria)
            for key, uids in self.search_results.items():
                if key in criteria:
                    return ("OK", [b" ".join(str(u).encode() for u in uids)])
            return ("OK", [b""])
        self.fetched.extend(args[0].split(","))
        return (
            "OK",
            [
                (f"UID {token}".encode(), b"From: x@example.com\r\n\r\nbody\r\n")
                for token in args[0].split(",")
            ],
        )


def _install(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeIMAP, tmp_path: Path
) -> None:
    monkeypatch.setenv("IETF_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        author_mail.imaplib, "IMAP4_SSL", lambda *a, **kw: fake  # type: ignore[misc]
    )


def _run(
    addresses: Sequence[str] = ("a@example.com",),
    months: Optional[int] = 12,
) -> int:
    return sync_author_mail("wg", ["alist"], list(addresses), months, Q)


# --- search construction --------------------------------------------------


def test_searches_from_for_every_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A person's older employer addresses carry real traffic; querying
    only one of them silently loses it."""
    fake = _FakeIMAP({})
    _install(monkeypatch, fake, tmp_path)
    _run(addresses=("a@example.com", "b@example.org"))
    assert any('FROM "a@example.com"' in c for c in fake.criteria)
    assert any('FROM "b@example.org"' in c for c in fake.criteria)


def test_applies_the_months_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({})
    _install(monkeypatch, fake, tmp_path)
    _run(months=6)
    assert all("SINCE" in c for c in fake.criteria)


def test_no_window_when_months_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({})
    _install(monkeypatch, fake, tmp_path)
    _run(months=None)
    assert all("SINCE" not in c for c in fake.criteria)


def test_quotes_are_escaped_in_search_terms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An address containing a quote must not break out of the term."""
    fake = _FakeIMAP({})
    _install(monkeypatch, fake, tmp_path)
    _run(addresses=('od"d@example.com',))
    assert 'od\\"d@example.com' in fake.criteria[0]


# --- scope ----------------------------------------------------------------


def test_pulls_only_their_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the FROM search runs — the context a persona needs is in the
    quotes those messages carry, not in the rest of the thread."""
    fake = _FakeIMAP({"FROM": [10, 11]})
    _install(monkeypatch, fake, tmp_path)
    assert _run() == 2
    assert len(fake.criteria) == 1
    assert "SUBJECT" not in fake.criteria[0]
    written = sorted(os.listdir(tmp_path / "imap-cache" / "wg" / "alist"))
    assert written == ["10.eml", "11.eml"]


def test_no_messages_means_no_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A list they never posted to costs one search and nothing else."""
    fake = _FakeIMAP({})
    _install(monkeypatch, fake, tmp_path)
    assert _run() == 0
    assert fake.fetched == []


def test_addresses_returning_the_same_uid_download_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({"FROM": [10]})
    _install(monkeypatch, fake, tmp_path)
    assert _run(addresses=("a@example.com", "b@example.org")) == 1


# --- resilience -----------------------------------------------------------


def test_already_cached_messages_are_not_refetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "imap-cache" / "wg" / "alist"
    cache.mkdir(parents=True)
    (cache / "10.eml").write_bytes(b"cached")
    fake = _FakeIMAP({"FROM": [10, 11]})
    _install(monkeypatch, fake, tmp_path)
    assert _run() == 1
    assert fake.fetched == ["11"]


def test_unselectable_folder_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a dozen speculative lists in play, one bad folder must not
    take the gather down."""
    fake = _FakeIMAP({}, select_ok=False)
    _install(monkeypatch, fake, tmp_path)
    notes: List[str] = []
    total = sync_author_mail(
        "wg", ["alist"], ["a@example.com"], 12, Q, note_fn=notes.append
    )
    assert total == 0
    assert notes and "no such folder" in notes[0]


def test_no_lists_or_addresses_is_a_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({"FROM": [1]})
    _install(monkeypatch, fake, tmp_path)
    assert sync_author_mail("wg", [], ["a@example.com"], 12, Q) == 0
    assert sync_author_mail("wg", ["alist"], [], 12, Q) == 0
    assert fake.criteria == []
