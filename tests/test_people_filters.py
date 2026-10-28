"""`read_digest(kind="people", org=…, email_domain=…)` and filter rejection.

Driven writer to reader: a registry is built, `write_people_digest` renders it,
and `tool_read_digest` reads it back, so the filters are tested against the
tables the writer actually emits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import mcp
from ietf_llm.digest.query import (
    SUPPORTED_FILTERS,
    normalise_domain,
    parse_md_tables,
    query_digest,
    unsupported_filter_message,
)
from ietf_llm.log import Verbosity
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.people import Registry, write_people_digest

from conftest import write_cache_file


def _write_digest() -> Path:
    r = Registry()
    r.add_email_message("Alice Author <alice@cloudflare.com>", None)
    r.add_document_author(
        "Alice Author", "alice@cloudflare.com", "draft-a", organization="Cloudflare"
    )
    # Cloudflare address, but no affiliation from any source.
    r.add_email_message("Bob Mailer <bob@cloudflare.com>", None)
    # Personal address, affiliation from a draft.
    r.add_email_message("Carol Writer <carol@gmail.com>", None)
    r.add_document_author(
        "Carol Writer", "carol@gmail.com", "draft-c", organization="Fastly"
    )
    # A subdomain, and a domain that merely ends in the same letters.
    r.add_email_message("Dan Subdomain <dan@mail.ietf.org>", None)
    r.add_email_message("Erin Lookalike <erin@notietf.org>", None)
    # GitHub only: no address at all.
    r.add_github_author("ghonly")
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET
    )
    assert path is not None
    return Path(path)


def _names(out: str) -> set[str]:
    return {
        line.split("|")[1].strip()
        for line in out.splitlines()
        if line.startswith("| ") and not line.startswith(("| Name", "| Role"))
    }


def test_digest_carries_an_email_domain_column_labelled_as_not_affiliation(
    isolated_home: Path,
) -> None:
    text = _write_digest().read_text()
    assert "| Email domain |" in text
    assert "NOT an " in text and "`email_domain=`" in text and "`org=`" in text


def test_org_matches_the_affiliation_column_only(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", org="cloudflare")
    assert "Alice Author" in out
    # Bob's address is at cloudflare.com, but org= does not look at addresses.
    assert "Bob Mailer" not in out
    assert "Carol Writer" not in out


def test_org_does_not_match_the_provenance_marks(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", org="draft")
    assert "Alice Author" not in out and "Carol Writer" not in out


def test_org_reports_how_many_rows_it_could_not_see(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", org="Fastly")
    assert "Carol Writer" in out
    # 6 actors: Alice and Carol have an affiliation; Bob, Dan, Erin and the
    # GitHub-only actor do not.
    assert "1 of 6 participant rows match the filters" in out
    assert "4 of 6 carry no affiliation signal" in out
    assert "reflects what is recorded" in out


def test_org_with_no_match_still_says_what_it_could_not_see(
    isolated_home: Path,
) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", org="nonexistent")
    assert "0 of 6 participant rows match the filters" in out
    assert "Alice Author" not in out


def test_email_domain_is_a_separate_signal(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="cloudflare.com")
    # Alice (with an affiliation) and Bob (without) are both at the domain.
    assert {"Alice Author", "Bob Mailer"} <= _names(out)
    assert "Carol Writer" not in out
    assert "1 of 6 have no email address recorded" in out


def test_email_domain_matches_subdomains_but_not_lookalikes(
    isolated_home: Path,
) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="@ietf.org")
    assert "Dan Subdomain" in out
    assert "Erin Lookalike" not in out


def test_email_domain_works_on_a_digest_without_the_column(
    isolated_home: Path,
) -> None:
    path = _write_digest()
    text = path.read_text()
    # A digest gathered before the column existed.
    lines = []
    drop = None
    for line in text.splitlines():
        if line.startswith("| Name") and "Email domain" in line:
            drop = [c.strip() for c in line.strip("|").split("|")].index("Email domain")
        if drop is not None and line.startswith("|"):
            cells = line.strip().strip("|").split("|")
            del cells[drop]
            line = "|" + "|".join(cells) + "|"
        elif not line.startswith("|"):
            drop = None
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="gmail.com")
    assert "Carol Writer" in out and "Alice Author" not in out


def test_email_domain_reads_the_address_column_of_leadership_rows(
    isolated_home: Path,
) -> None:
    # The leadership and authors tables have an `Email` column, not
    # `Email domain`; the filter must still see their addresses.
    r = Registry()
    r.add_email_message("Chair Person <chair@example.org>", None)
    r.add_datatracker_role("Chair Person", "chair@example.org", "Chair")
    r.add_email_message("Other Person <other@elsewhere.net>", None)
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="example.org")
    assert "## Working Group leadership" in out
    assert "Other Person" not in out


def test_filters_compose_with_the_existing_people_filters(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest(
        "wg", "people", email_domain="cloudflare.com", min_messages=1, limit=1
    )
    assert len(_names(out)) == 1


@pytest.mark.parametrize(
    "kind,filters,mentions",
    [
        ("people", {"author": "Hardie"}, "issues, pulls"),
        ("people", {"state": "open"}, "issues, pulls"),
        ("people", {"since": "2026-01-01"}, "threads, timeline"),
        ("people", {"sort": "activity"}, "issues, pulls, threads"),
        ("issues", {"org": "Nokia"}, "people"),
        ("threads", {"email_domain": "ietf.org"}, "people"),
        ("timeline", {"min_messages": 3}, "people, threads"),
        ("index", {"limit": 5}, "issues"),
        ("pulls", {"exclude_mechanical": True}, "timeline"),
    ],
)
def test_unsupported_filters_are_rejected_not_ignored(
    isolated_home: Path, kind: str, filters: dict, mentions: str
) -> None:
    write_cache_file(isolated_home, "wg", f"digests/{kind}.md", "# d\n")
    out = mcp.digest.tool_read_digest("wg", kind, **filters)
    name = next(iter(filters))
    assert f"`{name}` is not a filter for kind={kind!r}" in out
    assert mentions in out


def test_include_bodies_is_rejected_outside_issues_and_pulls(
    isolated_home: Path,
) -> None:
    write_cache_file(isolated_home, "wg", "digests/people.md", "# d\n")
    out = mcp.digest.tool_read_digest("wg", "people", include_bodies=True)
    assert "`include_bodies` is not a filter for kind='people'" in out


@pytest.mark.parametrize("kind", sorted(SUPPORTED_FILTERS))
def test_every_supported_filter_is_accepted(kind: str) -> None:
    for name in SUPPORTED_FILTERS[kind]:
        value: object = 1 if name in ("limit", "min_messages") else "x"
        if name == "exclude_mechanical":
            value = True
        assert unsupported_filter_message(kind, {name: value}) is None


def test_unset_values_are_not_treated_as_filters() -> None:
    unset = {"author": None, "state": "", "limit": -1, "exclude_mechanical": False}
    assert unsupported_filter_message("people", unset) is None
    assert unsupported_filter_message("index", unset) is None


def test_email_domain_counts_draft_and_role_addresses(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Zed Writer <zed@gmail.com>", None)
    r.add_document_author("Zed Writer", "zed@nokia.com", "draft-z", organization="Nokia")
    r.add_email_message("Chair Person <chair@example.org>", None)
    r.add_datatracker_role("Chair Person", "chair@elsewhere.net", "Chair")
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    assert "Zed Writer" in mcp.digest.tool_read_digest(
        "wg", "people", email_domain="nokia.com"
    )
    assert "Chair Person" in mcp.digest.tool_read_digest(
        "wg", "people", email_domain="elsewhere.net"
    )


def test_note_counts_matches_before_limit_and_other_filters(
    isolated_home: Path,
) -> None:
    r = Registry()
    for i in range(4):
        r.add_email_message(f"Person {i} <p{i}@nokia.com>", None)
    r.add_email_message("Elsewhere <e@other.org>", None)
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    out = mcp.digest.tool_read_digest(
        "wg", "people", email_domain="nokia.com", limit=1
    )
    assert "4 of 5 participant rows match the filters (1 shown, cut by `limit`)" in out
    assert len(_names(out)) == 1
    out = mcp.digest.tool_read_digest(
        "wg", "people", email_domain="nokia.com", min_messages=2
    )
    assert "0 of 5 participant rows match the filters" in out


def test_leadership_rows_are_not_counted_twice(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Chair Person <chair@example.org>", None)
    r.add_datatracker_role("Chair Person", "chair@example.org", "Chair")
    r.add_email_message("Other <o@example.org>", None)
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="example.org")
    assert "2 of 2 participant rows match the filters" in out


def test_a_pipe_in_a_name_does_not_shift_the_columns(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message('"Marco Serra | Mumble Group" <m@mumble.group>', None)
    r.add_email_message("Other <o@example.org>", None)
    path = write_people_digest(
        "wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET
    )
    assert path is not None
    assert "\\|" in Path(path).read_text()
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="mumble.group")
    assert "Marco Serra" in out and "Other" not in out
    assert "1 of 2 participant rows match" in out
    # The escape survives the re-render, so the table is not corrupted.
    assert "Marco Serra \\| Mumble Group" in out
    assert parse_md_tables(out)[0].rows[0][0] == "Marco Serra \\| Mumble Group"


@pytest.mark.parametrize(
    "typed",
    [
        "nokia.com",
        "  nokia.com  ",
        "NOKIA.COM",
        "nokia.com.",
        "@nokia.com",
        "*.nokia.com",
        "someone@nokia.com",
        "https://www.nokia.com/about",
    ],
)
def test_email_domain_accepts_what_people_type(isolated_home: Path, typed: str) -> None:
    r = Registry()
    r.add_email_message("Person <p@www.nokia.com>", None)
    r.add_email_message("Elsewhere <e@other.org>", None)
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    out = mcp.digest.tool_read_digest("wg", "people", email_domain=typed)
    assert "Person" in out and "Elsewhere" not in out


def test_whitespace_only_values_are_unset(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/issues.md", "# d\n")
    out = mcp.digest.tool_read_digest("wg", "issues", org="   ")
    assert "not a filter" not in out


def test_normalise_domain() -> None:
    assert normalise_domain("Chair@IETF.org") == "ietf.org"
    assert normalise_domain("http://mail.ietf.org:8080/x") == "mail.ietf.org:8080"


def test_org_matches_only_the_leading_affiliation(isolated_home: Path) -> None:
    r = Registry()
    r.add_email_message("Mover <m@now.example>", None)
    # Seven employers, newest first: the cell shows five and "(+2 earlier)".
    orgs = "Alpha Beta Gamma Delta Epsilon Zeta Eta".split()
    for year, org in zip(range(2024, 2017, -1), orgs):
        r.add_document_author(
            "Mover", "m@now.example", f"draft-{org}", organization=org, year=year
        )
    write_people_digest("wg", get_wg_file_cache_dir("wg"), r, verbose=Verbosity.QUIET)
    # The cell does list the earlier employers...
    assert "Beta (draft)" in mcp.digest.tool_read_digest("wg", "people")
    # ...but only the leading one answers org=.
    assert "Mover" in mcp.digest.tool_read_digest("wg", "people", org="alpha")
    for earlier in ("beta", "epsilon", "earlier", "draft"):
        assert "Mover" not in mcp.digest.tool_read_digest(
            "wg", "people", org=earlier
        )


def test_leading_affiliation_is_the_one_the_digest_ranks_first() -> None:
    text = (
        "# d\n\n## Mailing list only (2)\n\n"
        "| Name | Roles | Affiliation | Emails | Msgs |\n|---|---|---|---|---|\n"
        "| New | | Cisco (draft, github); Fastly (draft) (+1 earlier) | n@c.com | 1 |\n"
        "| Old | | Fastly (draft); Cisco (github) | o@f.com | 1 |\n"
    )
    assert "| New |" in _query(text, org="cisco")
    assert "| Old |" not in _query(text, org="cisco")
    assert "| Old |" in _query(text, org="fastly")
    assert "| New |" not in _query(text, org="fastly")


def _query(text: str, **filters: object) -> str:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "people.md"
        path.write_text(text)
        return query_digest(str(path), "people", **filters)


def test_org_match_is_case_insensitive_and_domain_cell_is_lowercased() -> None:
    text = (
        "# d\n\n## Mailing list only (1)\n\n"
        "| Name | Roles | Affiliation | Emails | Email domain | Msgs |\n"
        "|---|---|---|---|---|---|\n"
        "| A | | NOKIA (draft) | a@Nokia.COM | Nokia.COM | 1 |\n"
    )
    assert "| A |" in _query(text, org="nokia")
    assert "| A |" in _query(text, email_domain="nokia.com")


def test_a_digest_without_participant_tables_says_so() -> None:
    text = (
        "# d\n\n## Role and automated accounts (1)\n\n"
        "| Name | Emails |\n|---|---|\n| x | x@a.org |\n"
    )
    assert "no participant tables to filter" in _query(text, email_domain="a.org")


def test_zero_is_a_value_not_an_unset_filter(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "digests/index.md", "# d\n")
    out = mcp.digest.tool_read_digest("wg", "index", limit=0)
    assert "`limit` is not a filter" in out
    assert unsupported_filter_message("issues", {"min_messages": 0}) is not None
    assert unsupported_filter_message("issues", {"limit": 0}) is None


# Written out by hand so a slip in SUPPORTED_FILTERS is caught, not echoed.
_EXPECTED = {
    "index": set(),
    "issues": {"state", "label", "author", "limit", "sort"},
    "pulls": {"state", "label", "author", "limit", "sort"},
    "threads": {"since", "until", "min_messages", "limit", "subject", "sort"},
    "people": {"role", "min_messages", "limit", "org", "email_domain"},
    "timeline": {"since", "until", "event_kind", "exclude_mechanical", "limit"},
}


def test_supported_filters_table_is_exactly_what_each_kind_does() -> None:
    assert {k: set(v) for k, v in SUPPORTED_FILTERS.items()} == _EXPECTED
