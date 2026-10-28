"""`read_digest(kind="people", org=…, email_domain=…)` and filter rejection.

Driven writer to reader: a registry is built, `write_people_digest` renders it,
and `tool_read_digest` reads it back, so the filters are tested against the
tables the writer actually emits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ietf_llm import mcp
from ietf_llm.digest.query import SUPPORTED_FILTERS, unsupported_filter_message
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
    assert "1 of 6 participant rows matched" in out
    assert "4 of 6 participant rows carry no affiliation signal" in out
    assert "reflects what is recorded" in out


def test_org_with_no_match_still_says_what_it_could_not_see(
    isolated_home: Path,
) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", org="nonexistent")
    assert "0 of 6 participant rows matched" in out
    assert "Alice Author" not in out


def test_email_domain_is_a_separate_signal(isolated_home: Path) -> None:
    _write_digest()
    out = mcp.digest.tool_read_digest("wg", "people", email_domain="cloudflare.com")
    # Alice (with an affiliation) and Bob (without) are both at the domain.
    assert {"Alice Author", "Bob Mailer"} <= _names(out)
    assert "Carol Writer" not in out
    assert "carry no email address" in out


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
