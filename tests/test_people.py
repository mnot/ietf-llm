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


# --- Datatracker roles ----------------------------------------------------


def test_add_datatracker_role_merges_with_existing_email(
    isolated_home: Path,
) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    assert len(r.persons) == 1
    assert r.persons[0].roles == {"Chair"}


def test_add_datatracker_role_creates_new_when_unknown(
    isolated_home: Path,
) -> None:
    r = Registry()
    r.add_datatracker_role("Mike Bishop", "mbishop@example.com", "Area Director")
    assert len(r.persons) == 1
    assert r.persons[0].canonical_name == "Mike Bishop"
    assert r.persons[0].roles == {"Area Director"}
    # And future mail from them gets merged.
    r.add_email_message("Mike Bishop <mbishop@example.com>", None)
    assert len(r.persons) == 1


def test_leadership_orders_chairs_before_ads(isolated_home: Path) -> None:
    r = Registry()
    r.add_datatracker_role("Mike Bishop", "mbishop@x", "Area Director")
    r.add_datatracker_role("Mark Nottingham", "mnot@x", "Chair")
    r.add_datatracker_role("Suresh Krishnan", "suresh@x", "Chair")
    leaders = r.leadership()
    assert [p.canonical_name for p in leaders] == [
        "Mark Nottingham",
        "Suresh Krishnan",
        "Mike Bishop",
    ]


def test_people_digest_includes_leadership_section(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    r.add_datatracker_role("Mike Bishop", "mbishop@x", "Area Director")
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    assert "## Working Group leadership" in text
    # Chairs listed before ADs.
    chair_pos = text.find("Mark Nottingham")
    ad_pos = text.find("Mike Bishop")
    assert 0 < chair_pos < ad_pos


def test_add_document_author_records_authorship(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-ietf-aipref-vocab", is_editor=False,
    )
    assert len(r.persons) == 1
    person = r.persons[0]
    assert person.authored_documents == {"draft-ietf-aipref-vocab"}
    assert person.edited_documents == set()


def test_add_document_editor_separates_role(isolated_home: Path) -> None:
    r = Registry()
    r.add_document_author(
        "Martin Thomson", "mt@lowentropy.net",
        document="draft-ietf-aipref-vocab", is_editor=True,
    )
    person = r.persons[0]
    assert person.edited_documents == {"draft-ietf-aipref-vocab"}
    assert person.authored_documents == set()


def test_people_digest_includes_document_authors(isolated_home: Path) -> None:
    r = Registry()
    r.add_document_author(
        "Paul Keller", "paul@openfuture.eu",
        document="draft-ietf-aipref-vocab", is_editor=False,
    )
    r.add_document_author(
        "Martin Thomson", "mt@lowentropy.net",
        document="draft-ietf-aipref-vocab", is_editor=True,
    )
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    assert "## Document authors / editors" in text
    assert "draft-ietf-aipref-vocab (ed.)" in text  # Martin's editor mark
    assert "Paul Keller" in text


def test_add_document_author_records_affiliation(isolated_home: Path) -> None:
    r = Registry()
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-ietf-foo", organization="Cloudflare",
    )
    person = r.persons[0]
    assert person.affiliations == {"draft-ietf-foo": "Cloudflare"}


def test_affiliation_can_vary_across_drafts(isolated_home: Path) -> None:
    # User-raised case: same person ships under different orgs on
    # different drafts. The registry must preserve both so the
    # renderer can show "Cloudflare; Independent" rather than collapse
    # to whichever happens to be added last.
    r = Registry()
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-ietf-foo", organization="Cloudflare",
    )
    r.add_document_author(
        "Mark Nottingham", "mnot@mnot.net",
        document="draft-ietf-bar", organization="Independent",
    )
    person = r.persons[0]
    assert person.affiliations == {
        "draft-ietf-foo": "Cloudflare",
        "draft-ietf-bar": "Independent",
    }
    assert r.affiliation_tag("Mark Nottingham") == "Cloudflare; Independent"


def test_affiliation_tag_none_when_no_authorship(isolated_home: Path) -> None:
    # Mailing-list-only participants have no draft-derived affiliation;
    # the tag must be None rather than guessed from email domain.
    r = Registry()
    r.add_email_message("Alice <alice@example.com>", None)
    assert r.affiliation_tag("Alice") is None


def test_people_digest_renders_affiliation_column(isolated_home: Path) -> None:
    r = Registry()
    r.add_document_author(
        "Paul Keller", "paul@openfuture.eu",
        document="draft-ietf-aipref-vocab", organization="Open Future",
    )
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    # Affiliation column header AND the rendered value both appear.
    assert "Affiliation" in text
    assert "Open Future" in text


def test_people_digest_explains_affiliation_caveats(isolated_home: Path) -> None:
    # The digest preamble should warn the reader off email-domain
    # inference — affiliation is from the draft author block, not
    # the From header.
    r = Registry()
    r.add_email_message("Alice <alice@example.com>", None)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    assert "Authors' Addresses" in text
    assert "do not infer from email domain" in text.lower()


def test_role_column_appears_in_activity_table(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET,
    )
    assert path is not None
    text = Path(path).read_text()
    # The mail-only table has a Roles column; Mark's row should show "Chair".
    assert "Roles" in text
    # Find Mark's row in the activity table (not the leadership table).
    activity_section = text.split("## Mailing list only", 1)[1]
    assert "Chair" in activity_section


# --- role_tag (single short tag for chunk titles) -------------------------


def test_role_tag_returns_none_for_unknown(isolated_home: Path) -> None:
    r = Registry()
    assert r.role_tag("Nobody") is None


def test_role_tag_returns_none_for_known_person_without_role(
    isolated_home: Path,
) -> None:
    r = Registry()
    r.add_email_message("Alice Wonderland <alice@example.com>", None)
    assert r.role_tag("Alice Wonderland") is None


def test_role_tag_returns_chair(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mark Nottingham <mnot@mnot.net>", None)
    r.add_datatracker_role("Mark Nottingham", "mnot@mnot.net", "Chair")
    assert r.role_tag("Mark Nottingham") == "Chair"


def test_role_tag_shortens_area_director_to_ad(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mike Bishop <mb@example.com>", None)
    r.add_datatracker_role("Mike Bishop", "mb@example.com", "Area Director")
    assert r.role_tag("Mike Bishop") == "AD"


def test_role_tag_returns_all_hats(isolated_home: Path) -> None:
    # Multi-hat status is load-bearing: a Chair who is ALSO an Editor
    # of the draft being discussed has a procedural conflict worth
    # surfacing inline. role_tag returns all roles joined, leadership
    # first, then editor/author.
    r = Registry()
    r.add_email_message("Multi Role <multi@example.com>", None)
    r.add_datatracker_role("Multi Role", "multi@example.com", "Chair")
    r.add_document_author(
        "Multi Role", "multi@example.com",
        document="draft-foo", is_editor=True,
    )
    assert r.role_tag("Multi Role") == "Chair/Editor"


def test_role_tag_orders_leadership_first(isolated_home: Path) -> None:
    # Within multi-leadership cases, Chair beats AD beats Tech Advisor.
    r = Registry()
    r.add_datatracker_role("Person", "p@ex", "Area Director")
    r.add_datatracker_role("Person", "p@ex", "Chair")
    assert r.role_tag("Person") == "Chair/AD"


def test_role_tag_returns_editor_when_no_formal_role(
    isolated_home: Path,
) -> None:
    r = Registry()
    r.add_email_message("Martin Thomson <mt@example.com>", None)
    r.add_document_author(
        "Martin Thomson", "mt@example.com",
        document="draft-foo", is_editor=True,
    )
    assert r.role_tag("Martin Thomson") == "Editor"


def test_role_tag_returns_author_when_only_authorship(
    isolated_home: Path,
) -> None:
    r = Registry()
    r.add_email_message("Paul Keller <paul@example.com>", None)
    r.add_document_author(
        "Paul Keller", "paul@example.com",
        document="draft-foo", is_editor=False,
    )
    assert r.role_tag("Paul Keller") == "Author"
