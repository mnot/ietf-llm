"""Tests for identity normalisation (ietf_llm.people).

Specifically covers the cases the contract promises:
- DMARC-rewritten From addresses ungrey-listed back to local@domain
- Datatracker/mailman relay addresses ignored in favour of display name
- Display-name suffix stripping (" via Datatracker", "(IETF)")
- Multi-email same display name → one Person with both emails
- GitHub login matching an email local-part → linked to the same Person
- Each digest output looks structurally right
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ietf_llm.people import (
    Registry,
    _normalise_email,
    _normalise_name,
    build_registry,
    parse_from_header,
    write_people_digest,
)
from ietf_llm.utils import Verbosity, get_wg_file_cache_dir

from conftest import write_eml, write_github_archive, make_issue


# --- low-level normalisers --------------------------------------------------


def test_normalise_email_passes_plain() -> None:
    assert _normalise_email("mnot@mnot.net") == "mnot@mnot.net"


def test_normalise_email_lowercases() -> None:
    assert _normalise_email("Mnot@MNOT.net") == "mnot@mnot.net"


def test_normalise_email_unmunges_dmarc() -> None:
    assert _normalise_email("mnot=40mnot.net@dmarc.ietf.org") == "mnot@mnot.net"


def test_normalise_email_strips_pure_relay() -> None:
    # The relay address tells us nothing about who actually sent the
    # message; callers should fall back to display-name identity.
    assert _normalise_email("noreply@ietf.org") is None
    assert _normalise_email("noreply@datatracker.ietf.org") is None


def test_normalise_name_strips_via_suffix() -> None:
    assert _normalise_name("Mark Nottingham via Datatracker") == "Mark Nottingham"


def test_normalise_name_strips_ietf_paren() -> None:
    assert _normalise_name("Mirja Kuehlewind (IETF)") == "Mirja Kuehlewind"


def test_parse_from_header_extracts_components() -> None:
    name, addr = parse_from_header(
        "Mark Nottingham via Datatracker <mnot=40mnot.net@dmarc.ietf.org>"
    )
    assert name == "Mark Nottingham"
    assert addr == "mnot@mnot.net"


# --- Registry merging ------------------------------------------------------


def test_dmarc_variant_merges_with_plain(isolated_home: Path) -> None:
    r = Registry()
    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", when)
    r.add_email_message(
        "Mark Nottingham via Datatracker <mnot=40mnot.net@dmarc.ietf.org>", when
    )
    assert len(r.persons) == 1
    person = r.persons[0]
    assert person.canonical_name == "Mark Nottingham"
    # Both forms normalise to the same email; we capture the raw From
    # values in `raw_from_headers` for traceability.
    assert "mnot@mnot.net" in person.emails
    assert person.message_count == 2


def test_two_emails_same_name_merge(isolated_home: Path) -> None:
    r = Registry()
    when = datetime(2025, 6, 1, tzinfo=timezone.utc)
    r.add_email_message("James Rosewell <james@rosewell.me>", when)
    r.add_email_message("James Rosewell <james@51degrees.com>", when)
    assert len(r.persons) == 1
    person = r.persons[0]
    assert person.canonical_name == "James Rosewell"
    assert person.emails == {"james@rosewell.me", "james@51degrees.com"}


def test_relay_address_keyed_by_display_name(isolated_home: Path) -> None:
    # When the From address is a pure relay, identity has to come from
    # the display name. A subsequent real-email message with the same
    # name should merge.
    r = Registry()
    r.add_email_message(
        "Mark Nottingham via Datatracker <noreply@ietf.org>", None
    )
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    assert len(r.persons) == 1
    assert r.persons[0].emails == {"mnot@mnot.net"}


def test_distinct_names_stay_separate(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Alice Foo <alice@x>", None)
    r.add_email_message("Bob Bar <bob@x>", None)
    assert len(r.persons) == 2


def test_github_login_links_via_email_local_part(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_github_author("mnot")
    assert len(r.persons) == 1
    person = r.persons[0]
    assert person.canonical_name == "Mark Nottingham"
    assert person.github_logins == {"mnot"}


def test_github_only_actor_stays_separate(isolated_home: Path) -> None:
    r = Registry()
    r.add_github_author("randomuser")
    assert len(r.persons) == 1
    assert r.persons[0].canonical_name == "randomuser"


def test_canonical_for_email_resolves_all_forms(isolated_home: Path) -> None:
    r = Registry()
    when = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", when)
    assert (
        r.canonical_for_email(
            "Mark Nottingham via Datatracker <mnot=40mnot.net@dmarc.ietf.org>"
        )
        == "Mark Nottingham"
    )


def test_canonical_for_github_resolves(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_github_author("mnot")
    assert r.canonical_for_github("mnot") == "Mark Nottingham"
    # Unknown logins pass through unchanged.
    assert r.canonical_for_github("unknown") == "unknown"


# --- build_registry end-to-end on a synthetic cache ------------------------


def test_build_registry_aggregates_mail_and_github(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "Topic A", "Mark Nottingham <mnot@mnot.net>",
        "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    write_eml(
        isolated_home, "wg", "list", 2,
        "Re: Topic A",
        "Mark Nottingham via Datatracker "
        "<mnot=40mnot.net@dmarc.ietf.org>",
        "Tue, 02 Jan 2025 10:00:00 +0000",
    )
    write_github_archive(
        isolated_home, "wg", "org/repo",
        [make_issue(1, "Hi", author="mnot")],
    )
    registry = build_registry("wg", verbose=Verbosity.QUIET)
    # Mark across mail (2) + 1 GitHub issue → one Person.
    assert len(registry.persons) == 1
    person = registry.persons[0]
    assert person.canonical_name == "Mark Nottingham"
    assert person.message_count == 2
    assert person.issue_count == 1
    assert person.emails == {"mnot@mnot.net"}
    assert person.github_logins == {"mnot"}


# --- write_people_digest layout --------------------------------------------


def test_people_digest_buckets_actors_correctly(isolated_home: Path) -> None:
    write_eml(
        isolated_home, "wg", "list", 1,
        "Topic", "Mark Nottingham <mnot@mnot.net>",
        "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    write_eml(
        isolated_home, "wg", "list", 2,
        "Topic2", "Alice <alice@x>",
        "Mon, 01 Jan 2025 11:00:00 +0000",
    )
    write_github_archive(
        isolated_home, "wg", "org/repo",
        [
            make_issue(1, "Linked one", author="mnot"),
            make_issue(2, "Github only", author="ghOnly"),
        ],
    )
    registry = build_registry("wg", verbose=Verbosity.QUIET)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), registry, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    # Mark appears in the "linked" section.
    assert "Active on both mailing list and GitHub" in text
    assert "Mark Nottingham" in text
    assert "mnot@mnot.net" in text
    # Alice is mail-only.
    assert "Mailing list only" in text
    assert "alice@x" in text
    # ghOnly is github-only.
    assert "GitHub only" in text
    assert "ghOnly" in text


def test_people_digest_returns_none_for_empty(isolated_home: Path) -> None:
    registry = Registry()
    assert (
        write_people_digest(
            "wg",
            get_wg_file_cache_dir("wg"),
            registry,
            verbose=Verbosity.QUIET,
        )
        is None
    )
