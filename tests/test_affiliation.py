"""Tests for affiliation collapsing, recency ranking, and capping.

The failing case these come from: a `--author` corpus pulls a prolific
author's whole draft history, and the thread-file Participants line then
carried every organisation and every address fragment they had ever
written into an Authors' Addresses block.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.log import Verbosity
from ietf_llm.paths import drafts_dir, get_wg_file_cache_dir
from ietf_llm.people import Registry, _format_affiliations, _ingest_draft_authors
from ietf_llm.people.affiliation import group_affiliations, org_key


# --- org_key ---------------------------------------------------------------


def test_org_key_collapses_legal_forms_and_descriptors() -> None:
    keys = {
        org_key(v)
        for v in ("Akamai", "Akamai Technologies", "Akamai Technologies, Inc.")
    }
    assert keys == {"akamai"}


def test_org_key_collapses_case_and_punctuation() -> None:
    assert org_key("CloudFlare") == org_key("Cloudflare")
    assert org_key("Apple Inc.") == org_key("Apple, Inc")


def test_org_key_keeps_distinct_organisations_apart() -> None:
    # A trailing descriptor is only noise at the *end*; a qualified
    # subsidiary is its own organisation.
    assert org_key("Huawei Technologies Canada") != org_key("Huawei")
    assert org_key("Google") != org_key("Google Cloud")


def test_org_key_survives_an_all_noise_name() -> None:
    # Stripping must not leave an empty key that swallows everything.
    assert org_key("Systems") == "systems"


# --- grouping and ranking --------------------------------------------------


def _author(registry: Registry, doc: str, org: str, year: int) -> None:
    registry.add_document_author(
        "Mark Nottingham",
        "mnot@mnot.net",
        document=doc,
        organization=org,
        year=year,
    )


def test_near_duplicates_collapse_to_the_commonest_spelling(
    isolated_home: Path,
) -> None:
    r = Registry()
    _author(r, "draft-a", "Akamai", 2013)
    _author(r, "draft-b", "Akamai", 2014)
    _author(r, "draft-c", "Akamai Technologies, Inc.", 2012)
    groups = group_affiliations(r.persons[0])
    assert [g.display for g in groups] == ["Akamai"]
    assert groups[0].documents == 3
    assert groups[0].year == 2014


def test_affiliations_rank_newest_first(isolated_home: Path) -> None:
    r = Registry()
    _author(r, "draft-old", "Akamai", 2012)
    _author(r, "draft-new", "Cloudflare", 2024)
    _author(r, "draft-mid", "Fastly", 2019)
    assert [g.display for g in group_affiliations(r.persons[0])] == [
        "Cloudflare",
        "Fastly",
        "Akamai",
    ]


def test_undated_affiliation_sorts_after_dated_ones(isolated_home: Path) -> None:
    # A GitHub `company` field carries no date; a draft published last
    # year is the better evidence for "where are they now".
    r = Registry()
    _author(r, "draft-new", "Cloudflare", 2024)
    r.add_github_author("mnot")
    r.add_github_company("mnot", "Some Old Employer")
    assert [g.display for g in group_affiliations(r.persons[0])] == [
        "Cloudflare",
        "Some Old Employer",
    ]


def test_inline_tag_caps_and_counts_the_rest(isolated_home: Path) -> None:
    r = Registry()
    for i, (org, year) in enumerate(
        [
            ("Cloudflare", 2024),
            ("Fastly", 2019),
            ("Akamai", 2013),
            ("Rackspace", 2011),
            ("Yahoo! Inc.", 2008),
        ]
    ):
        _author(r, f"draft-{i}", org, year)
    assert (
        r.affiliation_tag("Mark Nottingham")
        == "Cloudflare; Fastly; Akamai (+2 earlier)"
    )


def test_inline_tag_has_no_overflow_note_when_under_the_cap(
    isolated_home: Path,
) -> None:
    r = Registry()
    _author(r, "draft-a", "Cloudflare", 2024)
    _author(r, "draft-b", "Fastly", 2019)
    assert r.affiliation_tag("Mark Nottingham") == "Cloudflare; Fastly"


def test_digest_cell_caps_and_keeps_provenance(isolated_home: Path) -> None:
    r = Registry()
    for i, (org, year) in enumerate(
        [
            ("Cloudflare", 2024),
            ("Fastly", 2019),
            ("Akamai", 2013),
            ("Rackspace", 2011),
            ("Yahoo! Inc.", 2008),
            ("BEA Systems", 2004),
        ]
    ):
        _author(r, f"draft-{i}", org, year)
    cell = _format_affiliations(r.persons[0])
    assert cell.startswith("Cloudflare (draft); Fastly (draft); Akamai (draft)")
    assert cell.endswith("(+1 earlier)")
    assert "BEA Systems" not in cell


# --- locality suppression --------------------------------------------------


def test_city_in_the_organisation_slot_is_suppressed(isolated_home: Path) -> None:
    # An author who states no <organization> puts their city where the
    # organisation would be. Nothing in the line itself says which it is
    # — but the same city appearing *below* an organisation line in
    # another draft settles it.
    r = Registry()
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-with-org", organization="Cloudflare",
        year=2024, address_lines=["Prahran VIC", "Australia"],
    )
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-without-org", organization="Prahran",
        year=2025, address_lines=["Australia"],
    )
    person = r.persons[0]
    # The raw record still holds what each document said …
    assert person.affiliations["draft:draft-without-org"] == "Prahran"
    # … but nothing renders it as an employer.
    assert [g.display for g in group_affiliations(person)] == ["Cloudflare"]
    assert r.affiliation_tag("Mark Nottingham") == "Cloudflare"


def test_locality_suppression_is_per_person(isolated_home: Path) -> None:
    # One person's home city must not blank out another person's
    # employer that happens to be spelled the same way.
    r = Registry()
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-a", organization="Cloudflare",
        year=2024, address_lines=["Reston", "United States of America"],
    )
    r.add_document_author(
        "Alice Chen", "alice@example.net",
        document="draft-b", organization="Reston", year=2024,
    )
    assert r.affiliation_tag("Alice Chen") == "Reston"


def test_merge_carries_localities_and_years(isolated_home: Path) -> None:
    # Two surface forms of one human, linked only later: the
    # corroboration and the dates one of them carries have to survive
    # the merge, or the suppression silently stops working.
    r = Registry()
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-b", organization="Prahran", year=2025,
    )
    # The corroborating draft lands on the other surface form, so it only
    # reaches the surviving Person through the merge.
    r.add_document_author(
        "M. Nottingham", "mnot@pobox.com",
        document="draft-a", organization="Cloudflare",
        year=2024, address_lines=["Prahran VIC"],
    )
    keep = r.person_for_email("mnot@mnot.net")
    drop = r.person_for_email("mnot@pobox.com")
    assert keep is not None and drop is not None and keep is not drop
    # `_merge_persons` is what every identity-linking pass funnels into.
    r._merge_persons(keep, drop)  # pylint: disable=protected-access
    assert "prahran" in keep.localities
    assert keep.affiliation_years["draft:draft-a"] == 2024
    assert [g.display for g in group_affiliations(keep)] == ["Cloudflare"]


# --- writer -> reader round-trip -------------------------------------------


def _write_draft(name: str, front_matter: str, author_block: str) -> None:
    """Drop a draft into the `wg` cache the way gather lays it out."""
    drafts = Path(drafts_dir(get_wg_file_cache_dir("wg")))
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"{name}.txt").write_text(
        f"{front_matter}\n\n\nAuthor's Address\n\n{author_block}\n"
    )


def test_ingest_from_real_draft_text(isolated_home: Path) -> None:
    # Drive the whole path the gather runs — draft text on disk through
    # the parser, the registry, and out to both renderers — rather than
    # hand-building the affiliation dict.
    _write_draft(
        "draft-ietf-wg-old-04",
        "HTTP                                                 M. Nottingham\n"
        "Internet-Draft                                    12 October 2013\n"
        "Expires: 15 April 2014",
        "   Mark Nottingham\n"
        "   Akamai Technologies, Inc.\n"
        "   Prahran VIC\n"
        "   Australia\n"
        "   Email: mnot@mnot.net\n",
    )
    _write_draft(
        "draft-ietf-wg-new-02",
        "HTTP                                                 M. Nottingham\n"
        "Internet-Draft                                        16 May 2025\n"
        "Expires: 17 November 2025",
        "   Mark Nottingham\n"
        "   Prahran\n"
        "   Australia\n"
        "   Email: mnot@mnot.net\n",
    )

    registry = Registry()
    _ingest_draft_authors("wg", registry, Verbosity.QUIET)
    person = registry.persons[0]
    assert person.authored_documents == {
        "draft-ietf-wg-old",
        "draft-ietf-wg-new",
    }
    # The recent draft states no organisation, so the only affiliation
    # rendered is the older employer — the city does not stand in for one,
    # because the older draft shows "Prahran VIC" below an org line.
    assert person.localities == {"prahran", "australia"}
    assert registry.affiliation_tag("Mark Nottingham") == "Akamai Technologies, Inc."
    assert _format_affiliations(person) == "Akamai Technologies, Inc. (draft)"
    # Years come off the front matter, not the `Expires` line six months
    # later, and each draft's raw value is still on the record.
    assert person.affiliation_years == {
        "draft:draft-ietf-wg-old": 2013,
        "draft:draft-ietf-wg-new": 2025,
    }


def test_organisation_does_not_suppress_itself(isolated_home: Path) -> None:
    # Authors routinely repeat their organisation inside their own postal
    # address. Recorded as a locality it would delete the only
    # affiliation the person has (observed: Peter Gutmann).
    r = Registry()
    r.add_document_author(
        "Peter Gutmann", "pgut001@cs.auckland.ac.nz",
        document="draft-a", organization="University of Auckland", year=2014,
        address_lines=[
            "Department of Computer Science",
            "University of Auckland",
            "New Zealand",
        ],
    )
    person = r.persons[0]
    assert "university of auckland" not in person.localities
    assert r.affiliation_tag("Peter Gutmann") == "University of Auckland"


def test_department_does_not_suppress_the_institution(isolated_home: Path) -> None:
    # Institution and department swap slots between drafts. Whichever
    # landed in the address must not erase the other (observed: Brian
    # Carpenter, Tim Chown, Nagendra Modadugu).
    r = Registry()
    r.add_document_author(
        "Brian Carpenter", "brian.e.carpenter@gmail.com",
        document="draft-old", organization="Department of Computer Science",
        year=2011, address_lines=["University of Auckland", "PB 92019",
                                  "Auckland, 1142", "New Zealand"],
    )
    r.add_document_author(
        "Brian Carpenter", "brian.e.carpenter@gmail.com",
        document="draft-new", organization="The University of Auckland",
        year=2025, address_lines=["School of Computer Science", "New Zealand"],
    )
    person = r.persons[0]
    assert "university of auckland" not in person.localities
    assert "school of computer science" not in person.localities
    assert [g.display for g in group_affiliations(person)] == [
        "The University of Auckland",
        "Department of Computer Science",
    ]


def test_numeric_company_name_keys_as_itself() -> None:
    # Stripping the descriptor must not reduce the name to a bare number.
    assert org_key("128 Technology") == "128 technology"
    assert org_key("128 Technology") != org_key("128 Systems")
