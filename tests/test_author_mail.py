"""Tests for sender-scoped mail: one person's messages, in their threads.

The IMAP server is replaced with a fake that records the SEARCH criteria
it was handed, so these cover the search terms themselves (FROM per
address, SINCE window, SUBJECT hydration) as well as the subject
normalisation and the two cases that deliberately can't be hydrated.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from ietf_llm.gather.sources import author_mail
from ietf_llm.gather.sources.author_mail import (
    MAX_UIDS_PER_SUBJECT,
    MIN_SUBJECT_CHARS,
    base_subject,
    sync_author_mail,
)
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


# --- base_subject ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Re: What is a Version?", "What is a Version?"),
        ("RE: re: Re: Nested", "Nested"),
        ("Fwd: Forwarded thing", "Forwarded thing"),
        ("Fw: Short form", "Short form"),
        ("[httpbis] Tagged subject", "Tagged subject"),
        ("Re: [httpbis] Both at once", "Both at once"),
        ("[a][b] Two tags", "Two tags"),
        ("  collapse   inner   space ", "collapse inner space"),
        ("Plain subject", "Plain subject"),
        ("", ""),
    ],
)
def test_base_subject(raw: str, expected: str) -> None:
    assert base_subject(raw) == expected


def test_base_subject_keeps_bracket_inside_subject() -> None:
    """Only a *leading* tag is stripped — brackets mid-subject are part
    of what makes the subject a usable thread key."""
    assert base_subject("Re: fix the [draft] wording") == "fix the [draft] wording"


# --- fake IMAP ------------------------------------------------------------


class _FakeIMAP:
    """Minimal IMAP4_SSL stand-in.

    `search_results` maps a substring of the SEARCH criteria to the UIDs
    returned for it; `subjects` maps a UID to its Subject header.
    """

    def __init__(
        self,
        search_results: Dict[str, List[int]],
        subjects: Dict[int, str],
        select_ok: bool = True,
    ) -> None:
        self.search_results = search_results
        self.subjects = subjects
        self.select_ok = select_ok
        self.criteria: List[str] = []
        self.fetched: List[str] = []

    # -- protocol surface used by the module under test --
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
        if command == "fetch" and "HEADER.FIELDS" in args[-1]:
            out: List[Any] = []
            for token in args[0].split(","):
                subject = self.subjects.get(int(token), "")
                out.append((b"header", f"Subject: {subject}\r\n\r\n".encode()))
            return ("OK", out)
        # Message download.
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
    fake: _FakeIMAP,
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
    fake = _FakeIMAP({}, {})
    _install(monkeypatch, fake, tmp_path)
    _run(fake, addresses=("a@example.com", "b@example.org"))
    assert any('FROM "a@example.com"' in c for c in fake.criteria)
    assert any('FROM "b@example.org"' in c for c in fake.criteria)


def test_applies_the_months_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({}, {})
    _install(monkeypatch, fake, tmp_path)
    _run(fake, months=6)
    assert all("SINCE" in c for c in fake.criteria)


def test_no_window_when_months_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _FakeIMAP({}, {})
    _install(monkeypatch, fake, tmp_path)
    _run(fake, months=None)
    assert all("SINCE" not in c for c in fake.criteria)


def test_quotes_are_escaped_in_search_terms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subject containing a quote must not break out of the term."""
    fake = _FakeIMAP(
        {"FROM": [1]}, {1: 'Re: the "quoted" thing in a long subject'}
    )
    _install(monkeypatch, fake, tmp_path)
    _run(fake)
    subject_terms = [c for c in fake.criteria if "SUBJECT" in c]
    assert subject_terms
    assert '\\"quoted\\"' in subject_terms[0]


# --- thread hydration -----------------------------------------------------


def test_hydrates_the_thread_around_their_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Their message alone is a fragment; the corpus wants the exchange."""
    fake = _FakeIMAP(
        {"FROM": [10], "SUBJECT": [10, 11, 12]},
        {10: "Re: a sufficiently long subject line"},
    )
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == 3
    written = sorted(os.listdir(tmp_path / "imap-cache" / "wg" / "alist"))
    assert written == ["10.eml", "11.eml", "12.eml"]


def test_no_messages_means_no_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A list they never posted to costs one search and nothing else."""
    fake = _FakeIMAP({}, {})
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == 0
    assert fake.fetched == []
    assert not any("SUBJECT" in c for c in fake.criteria)


def test_short_subject_is_not_hydrated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SUBJECT is a substring match, so hydrating "Agenda" would drag in
    every agenda mail the list ever carried. Their own message stays."""
    assert len("Agenda") < MIN_SUBJECT_CHARS
    fake = _FakeIMAP({"FROM": [10], "SUBJECT": [10, 11]}, {10: "Agenda"})
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == 1
    assert not any("SUBJECT" in c for c in fake.criteria)


def test_non_ascii_subject_is_not_hydrated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SEARCH SUBJECT would need a CHARSET negotiation the archive
    server doesn't reliably support."""
    fake = _FakeIMAP(
        {"FROM": [10], "SUBJECT": [10, 11]}, {10: "Re: a café discussion thread"}
    )
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == 1
    assert not any("SUBJECT" in c for c in fake.criteria)


def test_one_search_per_distinct_base_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ten replies in one thread are one thread, not ten searches."""
    fake = _FakeIMAP(
        {"FROM": [1, 2, 3], "SUBJECT": [1, 2, 3]},
        {
            1: "a sufficiently long subject line",
            2: "Re: a sufficiently long subject line",
            3: "Re: Re: [tag] a sufficiently long subject line",
        },
    )
    _install(monkeypatch, fake, tmp_path)
    _run(fake)
    assert len([c for c in fake.criteria if "SUBJECT" in c]) == 1


def test_oversized_thread_is_capped_but_keeps_their_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cap bounds *hydration*, so it takes the most recent run of a
    runaway subject match — and their own message, which is the reason
    the thread was pulled at all, survives being outside that run."""
    over = MAX_UIDS_PER_SUBJECT + 50
    fake = _FakeIMAP(
        {"FROM": [1], "SUBJECT": list(range(1, over + 1))},
        {1: "a sufficiently long subject line"},
    )
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == MAX_UIDS_PER_SUBJECT + 1
    written = os.listdir(tmp_path / "imap-cache" / "wg" / "alist")
    assert "1.eml" in written


# --- resilience -----------------------------------------------------------


def test_already_cached_messages_are_not_refetched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "imap-cache" / "wg" / "alist"
    cache.mkdir(parents=True)
    (cache / "10.eml").write_bytes(b"cached")
    fake = _FakeIMAP(
        {"FROM": [10], "SUBJECT": [10, 11]},
        {10: "a sufficiently long subject line"},
    )
    _install(monkeypatch, fake, tmp_path)
    assert _run(fake) == 1
    assert fake.fetched == ["11"]


def test_unselectable_folder_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a dozen speculative lists in play, one bad folder must not
    take the gather down."""
    fake = _FakeIMAP({}, {}, select_ok=False)
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
    fake = _FakeIMAP({"FROM": [1]}, {1: "a sufficiently long subject line"})
    _install(monkeypatch, fake, tmp_path)
    assert sync_author_mail("wg", [], ["a@example.com"], 12, Q) == 0
    assert sync_author_mail("wg", ["alist"], [], 12, Q) == 0
    assert fake.criteria == []
