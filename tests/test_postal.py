"""Tests for the organisation-line vs address-line classifier.

Every string here is taken from a real cached draft or RFC — the
regressions this guards are things the Authors' Addresses parser
actually stored as someone's employer.
"""

from __future__ import annotations

import pytest

from ietf_llm.gather.sources.postal import (
    is_country_line,
    locality_key,
    looks_like_postal_line,
    parse_document_year,
)


# --- looks_like_postal_line ------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        # Countries, as spelled in the corpus.
        "Australia",
        "United States of America",
        "USA",
        "US",
        "P.R. China",
        "FINLAND",
        "The Netherlands",
        "Czech Republic",
        # Street lines.
        "6011 W Courtyard Dr.",
        "33 Sandpiper Close",
        "1770 Massachusetts Ave, #322",
        "17/4 Krylatskaya st",
        "160, boulevard  de Valmy",
        "PO Box 170608",
        # City / region / postcode.
        "Burlingame, CA  94010",
        "Portland, OR  97229",
        "Prahran VIC",
        "Reston, VA",
        "31100 Toulouse",
        # Contact detail that leaked out of a malformed block.
        "703-883-6469",
        "margaret@thingmagic.com",
        "mailing list at iccrg@irtf.org and/or the authors:",
        "http://www.excite.com",
        # xml2rfc postal-line debris.
        "made in",
        "true, 1",
    ],
)
def test_address_lines_are_not_organisations(line: str) -> None:
    assert looks_like_postal_line(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "Cloudflare",
        "Fastly",
        "Akamai Technologies, Inc.",
        "greenbytes GmbH",
        "Google LLC",
        "Vigil Security, LLC",
        "Japan Registry Services Co., Ltd.",
        "The Varnish Cache Project",
        "Team Digitale, Italian Government",
        # Lowercase self-descriptions are real; only multi-word
        # lowercase fragments are debris.
        "independent",
        "sn3rd",
        "deSEC",
        "yes.com AG",
        "yes.com",
        # A digit in the name is not a house number, and a bracketed
        # abbreviation is not an Australian state.
        "ARTICLE 19",
        "Five9",
        "4K Associates / UC Irvine",
        "Thales Alenia Space (TAS)",
        "Alloc Init Labs - New York",
        "W3C/MIT",
    ],
)
def test_organisations_survive(line: str) -> None:
    assert looks_like_postal_line(line) is False


def test_blank_line_is_not_an_organisation() -> None:
    assert looks_like_postal_line("   ") is True


def test_is_country_line_needs_the_whole_line() -> None:
    assert is_country_line("Australia") is True
    # A country named inside a company is still a company.
    assert is_country_line("Huawei Technologies Canada") is False


# --- locality_key ----------------------------------------------------------


def test_locality_key_drops_region_and_postcode() -> None:
    # The point of the key: a city confirmed as "Prahran VIC" in one
    # draft has to match the bare "Prahran" misread as an org in another.
    assert locality_key("Prahran VIC") == locality_key("Prahran") == "prahran"
    assert locality_key("Burlingame, CA  94010") == "burlingame"


def test_locality_key_keeps_multi_word_places() -> None:
    assert locality_key("Mountain View") == "mountain view"


# --- parse_document_year ---------------------------------------------------


def test_year_from_draft_front_matter() -> None:
    text = """
HTTP                                                       M. Nottingham
Internet-Draft                                               16 May 2025
Intended status: Standards Track
Expires: 17 November 2025


                           HTTP Cache Groups
"""
    # The Expires date is six months out; the real date is what counts.
    assert parse_document_year(text) == 2025


def test_year_from_rfc_front_matter() -> None:
    text = """
Internet Engineering Task Force (IETF)                     M. Nottingham
Request for Comments: 9651                                    Cloudflare
Obsoletes: 8941                                                P-H. Kamp
Category: Standards Track                      The Varnish Cache Project
ISSN: 2070-1721                                           September 2024
"""
    assert parse_document_year(text) == 2024


def test_year_ignores_expires_sharing_a_line() -> None:
    # Older drafts put both dates on one line, in two columns.
    text = (
        "Internet-Draft                                          R. Housley\n"
        "Expires: 12 April 2018                             12 October 2017\n"
    )
    assert parse_document_year(text) == 2017


def test_year_none_when_front_matter_has_no_date() -> None:
    assert parse_document_year("Some Working Group\nInternet-Draft\n") is None
