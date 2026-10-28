"""Name keys, role-account detection and the identity splits they close.

The splits are the ones seen in `aipref`'s people digest: `Deen, Glenn
(Comcast Cable)` / `Glenn Deen`, `Jo Levy` / `Jo-Levy`, `Eric Vyncke
(evyncke)` / `Éric Vyncke`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm.digest.query import query_digest
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people import Person, Registry, build_registry, write_people_digest
from ietf_llm.people.linking import link_github_logins_by_name
from ietf_llm.people.names import is_role_account, login_as_name, name_key

from conftest import make_issue, write_eml, write_github_archive


@pytest.mark.parametrize(
    "variants",
    [
        ["Glenn Deen", "Deen, Glenn", "Deen, Glenn (Comcast Cable)", "glenn deen"],
        ["Jo Levy", "Jo-Levy", "jo_levy", "Jo  Levy"],
        ["Eric Vyncke", "Éric Vyncke", "Eric Vyncke (evyncke)", "ÉRIC VYNCKE"],
        ["Marco Davids", "Marco Davids (IETF IMAP)"],
    ],
)
def test_name_key_folds_variants_together(variants: list[str]) -> None:
    assert len({name_key(v) for v in variants}) == 1


@pytest.mark.parametrize(
    "first,second",
    [
        ("L. Le Meur", "Laurent Le Meur"),
        ("Mark Smith", "Mary Smith"),
        ("Smith, Jr.", "Jr. Smith"),
        ("ガ", "カ"),
        ("-", "_"),
        ("—", "-"),
    ],
)
def test_name_key_keeps_distinct_names_apart(first: str, second: str) -> None:
    assert name_key(first) != name_key(second)


def test_name_key_folds_latin_accents_only() -> None:
    assert name_key("Müller") == "muller"
    assert name_key("Šimon Ščerbak") == "simon scerbak"


def test_name_key_of_bare_separators_is_not_empty() -> None:
    assert name_key("-") and name_key("_") and name_key("—")


def test_name_key_leaves_a_name_that_is_only_a_parenthetical() -> None:
    assert name_key("(IETF)") == "(ietf)"


@pytest.mark.parametrize(
    "login,expected",
    [("Jo-Levy", "jo levy"), ("mark_smith", "mark smith"), ("mirjak", None),
     ("kmadhavan-msft", "kmadhavan msft"), ("dev-1", None), ("a-b", None)],
)
def test_login_as_name(login: str, expected: str | None) -> None:
    assert login_as_name(login) == expected


def _person(name: str, **kw: set[str]) -> Person:
    return Person(canonical_name=name, **kw)


@pytest.mark.parametrize(
    "person",
    [
        _person("internet-drafts@ietf.org", emails={"internet-drafts@ietf.org"}),
        _person("IETF Secretariat", emails={"agenda@ietf.org"}),
        _person("The IESG"),
        _person("IESG Secretary", emails={"iesg-secretary@ietf.org"}),
        _person("IETF Meeting Session Request Tool"),
        _person("Repository Activity Summary Bot"),
        _person("rfc-editor@rfc-editor.org", emails={"rfc-editor@rfc-editor.org"}),
        _person("Honda", emails={"noreply@honda.co.uk"}),
        _person("Liaison Statement Management Tool"),
        _person("IAB Chair"),
        _person("GitHub", emails={"noreply@github.com"}),
        _person("github-actions[bot]", github_logins={"github-actions[bot]"}),
        _person("x", github_logins={"dependabot"}),
    ],
)
def test_role_accounts_detected(person: Person) -> None:
    assert is_role_account(person)


@pytest.mark.parametrize(
    "person",
    [
        _person("Mark Nottingham", emails={"mnot@ietf.org"}),
        _person("Abbot Costello", github_logins={"abbot"}),
        _person("Jane Bot"),
        # A GitHub-relayed sender is a person; so is anyone who also posts
        # from a real address.
        _person("Med Boucadair", emails={"noreply@github.com"}),
        _person("Jane Doe", emails={"jane@corp.example", "noreply@corp.example"}),
        _person("Roberto", github_logins={"robot-lover"}),
        _person("Jo Chair", emails={"chair@example.com"}),
        # A role or a document credit makes someone a person, whatever the
        # mailbox looks like.
        _person("Secretary Person", emails={"secretariat@ietf.org"}, roles={"Chair"}),
        _person("Author", emails={"iesg@ietf.org"}, authored_documents={"draft-x"}),
    ],
)
def test_people_not_mistaken_for_role_accounts(person: Person) -> None:
    assert not is_role_account(person)


def test_mail_names_differing_in_form_are_one_person() -> None:
    r = Registry()
    r.add_email_message('"Deen, Glenn (Comcast Cable)" <glenn.deen@nbcuni.com>', None)
    r.add_email_message("Glenn Deen <gdeen@example.net>", None)
    r.add_email_message("Eric Vyncke (evyncke) <evyncke@cisco.com>", None)
    r.add_email_message("Éric Vyncke <>", None)
    assert len(r.persons) == 2
    deen = r.person_for_name("Glenn Deen")
    assert deen is not None
    assert deen.message_count == 2
    assert deen.emails == {"glenn.deen@nbcuni.com", "gdeen@example.net"}
    assert r.canonical_for_email("Éric Vyncke <>") == "Eric Vyncke (evyncke)"


def test_github_real_name_links_across_name_forms() -> None:
    r = Registry()
    r.add_email_message('"Deen, Glenn (Comcast Cable)" <glenn.deen@nbcuni.com>', None)
    r.add_github_author("gitnnelg")
    assert r.link_github_identity("gitnnelg", "Glenn Deen", []) == "linked"
    assert len(r.persons) == 1
    assert r.persons[0].github_logins == {"gitnnelg"}


def _link(r: Registry) -> None:
    link_github_logins_by_name(r, Verbosity.QUIET)


def test_login_that_reads_as_a_name_links_to_the_mail_person() -> None:
    r = Registry()
    r.add_email_message("Jo Levy <jlevy@nortonlaw.com>", None)
    r.add_github_author("Jo-Levy")
    r.add_github_author("Jo-Levy")
    _link(r)
    assert len(r.persons) == 1
    person = r.persons[0]
    assert person.canonical_name == "Jo Levy"
    assert person.github_logins == {"Jo-Levy"}
    assert (person.message_count, person.issue_count) == (1, 2)


def test_login_linking_declines_when_there_is_nothing_safe_to_link() -> None:
    r = Registry()
    r.add_email_message("Mirja Kuehlewind <ietf@kuehlewind.net>", None)
    r.add_github_author("mirjak")
    # Name-shaped, but nobody on the list by that name.
    r.add_github_author("Alex-Chen")
    # Matches a name only held by a stub with no mail signal.
    r.add_datatracker_role("Pat Jones", None, "Chair")
    r.add_github_author("Pat-Jones")
    _link(r)
    assert len(r.persons) == 5


def test_login_linking_leaves_a_login_the_api_named() -> None:
    # `Mark-Smith` has a profile name, so it is no longer a bare login and
    # must not be matched on the login's shape.
    r = Registry()
    r.add_email_message("Mark Smith <mark@example.org>", None)
    r.add_github_author("Mark-Smith")
    assert r.link_github_identity("Mark-Smith", "Marcus Taylor", []) == "named"
    _link(r)
    assert sorted(p.canonical_name for p in r.persons) == [
        "Marcus Taylor",
        "Mark Smith",
    ]


def test_login_linking_declines_a_name_two_mail_people_share() -> None:
    r = Registry()
    first = r.add_email_message("John Smith <john@one.example>", None)
    second = r.add_email_message("Jon Smith <jon@two.example>", None)
    assert first is not None and second is not None
    second.canonical_name = "John Smith"
    r.add_github_author("John-Smith")
    _link(r)
    assert len(r.persons) == 3


def test_login_linking_is_not_repeated_for_a_person_with_two_logins() -> None:
    r = Registry()
    r.add_email_message("Jo Levy <jlevy@nortonlaw.com>", None)
    gh = r.add_github_author("Jo-Levy")
    assert gh is not None
    r.add_github_author("jo_levy")
    # Both logins resolve to the same name; the second must not re-merge.
    assert len(r.persons) == 3
    _link(r)
    assert len(r.persons) == 1
    assert r.persons[0].issue_count == 2
    assert r.persons[0].github_logins == {"Jo-Levy", "jo_levy"}


def _seed_corpus(home: Path) -> None:
    stamp = "Mon, 01 Jan 2025 10:00:00 +0000"
    write_eml(home, "wg", "list", 1, "Topic", "Jo Levy <jlevy@nortonlaw.com>", stamp)
    write_eml(
        home, "wg", "list", 2, "Topic", "internet-drafts@ietf.org", stamp
    )
    write_eml(
        home, "wg", "list", 3, "Meeting", "IETF Secretariat <agenda@ietf.org>", stamp
    )
    write_eml(
        home, "wg", "list", 4, "Topic",
        '"Deen, Glenn (Comcast Cable)" <glenn.deen@nbcuni.com>', stamp,
    )
    write_github_archive(
        home, "wg", "org/repo",
        [
            make_issue(1, "Mine", author="Jo-Levy"),
            make_issue(2, "Bot", author="github-actions[bot]"),
        ],
    )


def test_digest_lists_role_accounts_apart_from_participants(
    isolated_home: Path,
) -> None:
    _seed_corpus(isolated_home)
    registry = build_registry("wg", verbose=Verbosity.QUIET)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), registry, verbose=Verbosity.QUIET
    )
    assert path is not None
    text = Path(path).read_text()
    participants, role_section = text.split("## Role and automated accounts", 1)
    assert "plus 3 role and automated accounts" in participants
    for name in ("internet-drafts@ietf.org", "IETF Secretariat", "github-actions[bot]"):
        assert name in role_section
        assert name not in participants
    # Jo Levy and the GitHub-only `Jo-Levy` are one row with both surfaces.
    assert participants.count("Jo Levy") == 1
    assert "Jo-Levy" in participants
    assert "## Active on both mailing list and GitHub (1)" in participants


def test_role_section_survives_the_digest_reader(isolated_home: Path) -> None:
    _seed_corpus(isolated_home)
    registry = build_registry("wg", verbose=Verbosity.QUIET)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), registry, verbose=Verbosity.QUIET
    )
    assert path is not None
    out = query_digest(path, "people", min_messages=1)
    assert "## Role and automated accounts" in out
    assert "Jo Levy" in out
