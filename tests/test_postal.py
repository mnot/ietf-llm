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
    names_an_organisation,
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
        # A leading number alone is not a street: these are companies,
        # and both have drafts in the record.
        "128 Technology",
        "802 Secure",
    ],
)
def test_organisations_survive(line: str) -> None:
    assert looks_like_postal_line(line) is False


def test_street_needs_more_than_a_leading_number() -> None:
    # Three or more words, or a thoroughfare word, is what separates a
    # street line from a numeric company name.
    assert looks_like_postal_line("100 Sandpiper Close") is True
    assert looks_like_postal_line("17/4 Krylatskaya st") is True
    assert looks_like_postal_line("128 Technology") is False


# --- names_an_organisation -------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "University of Auckland",
        "Department of Computer Science",
        "School of Electronics and Computer Science",
        "Vigil Security, LLC",
        "Akamai Technologies",
    ],
)
def test_institution_lines_are_not_places(line: str) -> None:
    # These turn up *below* an organisation line in one draft and *in*
    # the organisation slot in another. Recording them as localities
    # would let one draft's address erase the other draft's employer.
    assert names_an_organisation(line) is True


@pytest.mark.parametrize(
    "line", ["Prahran VIC", "Melbourne", "Australia", "Mountain View", "Reston, VA"]
)
def test_place_lines_are_still_places(line: str) -> None:
    assert names_an_organisation(line) is False


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


def test_year_ignores_dates_below_the_header_block() -> None:
    # The IETF Trust boilerplate and the abstract both sit inside the
    # first 30 lines and both carry dates that are not the document's.
    # draft-rpc-errata-process-05 came out dated 2007 this way.
    text = (
        "RFC Series Working Group                                    J. Klensin\n"
        "Internet-Draft                                           18 March 2026\n"
        "Expires: 19 September 2026\n"
        "\n"
        "\n"
        "                     Errata Processing for the RFC\n"
        "\n"
        "Abstract\n"
        "\n"
        "   This document revises the process described in November 2007.\n"
    )
    assert parse_document_year(text) == 2026


def test_year_ignores_unlabelled_expiry_after_the_real_date() -> None:
    # "Expiration Date" does not start with "expires"; and where a header
    # carries two dates the document's own comes first.
    text = (
        "TLS Working Group                                            S. Moriai\n"
        "Internet-Draft                                          October 2004\n"
        "Expiration Date: March 2005\n"
    )
    assert parse_document_year(text) == 2004


def test_year_survives_a_byte_order_mark_before_the_header() -> None:
    # RFC text files open with a BOM on its own line; it must not read as
    # the blank that terminates the header block.
    text = "﻿\n\n\nInternet Engineering Task Force (IETF)     M. Nottingham\nISSN: 2070-1721                              September 2024\n"
    assert parse_document_year(text) == 2024
