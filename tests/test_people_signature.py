"""Affiliation from a display-name parenthetical (`signature` source).

The cases are the parentheticals found in the real mail caches: organisations
that must be kept, and handles, nicknames, codes and other names that must not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people import Registry, build_registry, write_people_digest
from ietf_llm.people.digest import _format_affiliations
from ietf_llm.people.names import trailing_parentheticals, without_parentheticals
from ietf_llm.people.signature import apply_signature_affiliations

from conftest import write_eml


def _signature_orgs(registry: Registry) -> dict[str, set[str]]:
    return {
        p.canonical_name: {
            v for k, v in p.affiliations.items() if k.startswith("signature:")
        }
        for p in registry.persons
    }


def _run(*headers: str) -> dict[str, set[str]]:
    r = Registry()
    for header in headers:
        r.add_email_message(header, None)
    apply_signature_affiliations(r, Verbosity.QUIET)
    return _signature_orgs(r)


def test_parenthetical_helpers() -> None:
    assert trailing_parentheticals("Deen, Glenn (Comcast Cable)") == ["Comcast Cable"]
    assert trailing_parentheticals("A (b) (C)") == ["C", "b"]
    assert trailing_parentheticals("Plain Name") == []
    assert without_parentheticals("Deen, Glenn (Comcast Cable)") == "Deen, Glenn"


@pytest.mark.parametrize(
    "header,org",
    [
        ('"Deen, Glenn (Comcast Cable)" <glenn.deen@nbcuni.com>', "Comcast Cable"),
        ("Martin Pels (RIPE NCC) <mpels@ripe.net>", "RIPE NCC"),
        ("Christian Veenman (NCSC-NL) <c@ncsc.nl>", "NCSC-NL"),
        ('"Chalk, Ollie (DSIT)" <o@dsit.gov.uk>', "DSIT"),
        ("Thomas Corte (TANGO support) <t@tango.example>", "TANGO support"),
        ("Andrew Stone (Nokia) <andrew.stone@nokia.com>", "Nokia"),
    ],
)
def test_organisations_are_recorded(header: str, org: str) -> None:
    (orgs,) = _run(header).values()
    assert orgs == {org}


@pytest.mark.parametrize(
    "header",
    [
        "Eric Vyncke (evyncke) <evyncke@cisco.com>",
        "Dongjie (Jimmy) <d@example.cn>",
        "maqiufang (A) <m@example.cn>",
        '"Dang, Quynh H. (Fed)" <q@nist.example>',
        "Pengshuping (Peng Shuping) <p@example.cn>",
        "Liuchunchi(Peter) <l@example.cn>",
        "Marco Davids (IETF IMAP) <m@example.nl>",
        "Natasha Carroll (via Google Photos) <n@example.com>",
        '"Kennedy, Smith (Wireless & Standards Architect)" <s@example.com>',
        "Xialiang(Frank, IP Security Standard) <x@example.cn>",
        "刘大鹏(联通集团本部) <l@chinaunicom.example>",
        "Flavius-Romeo MAZILU (123666) <f@example.ro>",
        "Pof Magicfingers (Giovanni Olivera) <p@example.org>",
        # A lone capitalised word with nothing to corroborate it.
        "Arnab Mallick (Chatterjee) <a@example.in>",
        '"Dwight, Timothy M (Tim)" <t@example.com>',
    ],
)
def test_other_parentheticals_are_not_organisations(header: str) -> None:
    (orgs,) = _run(header).values()
    assert orgs == set()


def test_a_lone_word_needs_corroboration() -> None:
    # No domain, draft or second user vouches for it...
    assert list(_run("Rifaat Shek (Ciena) <r@example.org>").values()) == [set()]
    # ...an email domain carrying the word does,
    assert list(_run("Rifaat Shek (Ciena) <r@ciena.com>").values()) == [{"Ciena"}]
    # ...as does a second person using the same parenthetical.
    both = _run("A One (Acme) <a@one.example>", "B Two (Acme) <b@two.example>")
    assert both == {"A One (Acme)": {"Acme"}, "B Two (Acme)": {"Acme"}}


def test_an_organisation_another_source_found_corroborates() -> None:
    r = Registry()
    r.add_email_message("A One (Acme) <a@one.example>", None)
    other = r.add_document_author(
        "C Three", "c@three.example", "draft-x", organization="Acme Corp"
    )
    assert other is not None
    apply_signature_affiliations(r, Verbosity.QUIET)
    assert _signature_orgs(r)["A One (Acme)"] == {"Acme"}


def test_a_person_s_own_name_and_other_peoples_names_are_not_organisations() -> None:
    r = Registry()
    r.add_email_message('"Lear, Eliot" <e@example.org>', None)
    r.add_email_message("Some Mailbox (Eliot Lear) <m@example.org>", None)
    apply_signature_affiliations(r, Verbosity.QUIET)
    assert _signature_orgs(r)["Some Mailbox (Eliot Lear)"] == set()


def test_role_accounts_get_no_signature_affiliation() -> None:
    (orgs,) = _run(
        "Independent Submissions Editor (Eliot Lear Cable) <rfc-ise@rfc-editor.org>"
    ).values()
    assert orgs == set()


def test_merged_people_keep_every_parenthetical() -> None:
    r = Registry()
    r.add_email_message("Jo Levy (RIPE NCC) <j@ripe.example>", None)
    r.add_email_message('"Levy, Jo" <j2@other.example>', None)
    assert len(r.persons) == 1
    assert r.persons[0].name_parentheticals == {"RIPE NCC"}


def test_signature_affiliation_ranks_after_a_draft_and_agrees_with_it() -> None:
    r = Registry()
    r.add_email_message("Jo Levy (Fastly Technologies) <j@fastly.example>", None)
    r.add_document_author(
        "Jo Levy", "j@fastly.example", "draft-x", organization="Fastly", year=2024
    )
    r.add_document_author(
        "Jo Levy", "j@fastly.example", "draft-y", organization="Cloudflare", year=2020
    )
    apply_signature_affiliations(r, Verbosity.QUIET)
    cell = _format_affiliations(r.persons[0])
    assert cell == "Fastly (draft, signature); Cloudflare (draft)"


def test_writer_to_reader_round_trip(isolated_home: Path) -> None:
    stamp = "Mon, 01 Jan 2025 10:00:00 +0000"
    write_eml(
        isolated_home, "wg", "list", 1, "Topic",
        '"Deen, Glenn (Comcast Cable)" <glenn.deen@nbcuni.com>', stamp,
    )
    write_eml(
        isolated_home, "wg", "list", 2, "Topic",
        "Eric Vyncke (evyncke) <evyncke@cisco.com>", stamp,
    )
    registry = build_registry("wg", verbose=Verbosity.QUIET)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), registry, verbose=Verbosity.QUIET
    )
    assert path is not None
    text = Path(path).read_text()
    assert "Comcast Cable (signature)" in text
    assert "evyncke (" not in text.split("## Mailing list only", 1)[1]
    assert "`(signature)`" in text
