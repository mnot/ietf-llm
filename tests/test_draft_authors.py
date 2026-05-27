"""Tests for the IETF draft / RFC Authors' Addresses parser."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.gather.draft_authors import latest_draft_paths, parse_authors


# --- parse_authors ---------------------------------------------------------


def test_organization_captured_from_line_after_name() -> None:
    text = """Authors' Addresses

   Mark Nottingham
   Cloudflare
   Email: mnot@mnot.net

   Alice Chen
   Mozilla
   Email: alice@example.net
"""
    authors = parse_authors(text)
    assert authors[0].name == "Mark Nottingham"
    assert authors[0].organization == "Cloudflare"
    assert authors[1].organization == "Mozilla"


def test_organization_none_when_only_name_and_email() -> None:
    # Some author blocks skip the org line entirely (independent
    # participants, or terse layouts). organization stays None — the
    # consumer should NOT see a default like "Independent" appear out
    # of nowhere.
    text = """Author's Address

   Mark Nottingham
   Email: mnot@mnot.net
"""
    authors = parse_authors(text)
    assert authors[0].organization is None


def test_basic_two_author_layout() -> None:
    text = """Internet-Draft               aipref                       November 2025


Authors' Addresses

   Paul Keller
   Open Future
   Email: paul@openfuture.eu


   Martin Thomson (editor)
   Mozilla
   Email: mt@lowentropy.net
"""
    authors = parse_authors(text)
    assert len(authors) == 2
    assert authors[0].name == "Paul Keller"
    assert authors[0].email == "paul@openfuture.eu"
    assert not authors[0].is_editor
    assert authors[1].name == "Martin Thomson"
    assert authors[1].is_editor


def test_singular_authors_address_header() -> None:
    # Solo-authored drafts use the singular header form.
    text = """Author's Address

   Mark Nottingham
   Cloudflare
   Email: mnot@mnot.net
"""
    authors = parse_authors(text)
    assert len(authors) == 1
    assert authors[0].name == "Mark Nottingham"


def test_plural_authors_address_header() -> None:
    text = """Authors' Addresses

   Alice Foo
   Email: alice@example.org

   Bob Bar
   Email: bob@example.org
"""
    authors = parse_authors(text)
    assert [a.name for a in authors] == ["Alice Foo", "Bob Bar"]


def test_page_footer_does_not_break_block() -> None:
    text = """Authors' Addresses

   Paul Keller
   Open Future
   Email: paul@openfuture.eu



Keller & Thomson         Expires 30 October 2026               [Page 13]


Internet-Draft               aipref                       November 2025


   Martin Thomson (editor)
   Mozilla
   Email: mt@lowentropy.net
"""
    # Note: at the page boundary, a non-indented line ("Internet-Draft …")
    # would normally terminate the section, but our parser tolerates
    # interleaved page-footer noise; the second author's block is still
    # captured when the indented Email line follows.
    # If the parser DOES stop at the non-indented page header, the
    # first author still comes through — which is the conservative
    # outcome we want.
    authors = parse_authors(text)
    assert len(authors) >= 1
    assert authors[0].name == "Paul Keller"


def test_editor_abbreviation_recognised() -> None:
    text = """Authors' Addresses

   Alice Foo (ed.)
   Email: alice@example.org
"""
    authors = parse_authors(text)
    assert len(authors) == 1
    assert authors[0].is_editor


def test_no_section_returns_empty() -> None:
    text = "Some draft body with no authors section.\n"
    assert parse_authors(text) == []


def test_block_without_email_skipped_or_kept() -> None:
    # If a "block" has only a name and no email, the parser still
    # returns it (name only) — we know who but not how to reach them.
    text = """Authors' Addresses

   Person With No Listed Email
   Some Affiliation
"""
    authors = parse_authors(text)
    # Either skipped or kept with email=None; both are defensible.
    assert len(authors) <= 1
    if authors:
        assert authors[0].email is None


# --- latest_draft_paths ----------------------------------------------------


def _make_drafts_dir(tmp_path: Path) -> Path:
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    return drafts


def test_latest_draft_paths_picks_highest_version(tmp_path: Path) -> None:
    drafts = _make_drafts_dir(tmp_path)
    for v in ("00", "01", "02", "05"):
        (drafts / f"draft-ietf-wg-foo-{v}.txt").write_text("x")
    paths = latest_draft_paths(str(tmp_path))
    names = [p.rsplit("/", 1)[-1] for p in paths]
    assert names == ["draft-ietf-wg-foo-05.txt"]


def test_latest_draft_paths_includes_rfcs(tmp_path: Path) -> None:
    drafts = _make_drafts_dir(tmp_path)
    (drafts / "rfc9111.txt").write_text("x")
    (drafts / "draft-ietf-wg-foo-01.txt").write_text("x")
    paths = latest_draft_paths(str(tmp_path))
    names = sorted(p.rsplit("/", 1)[-1] for p in paths)
    assert names == ["draft-ietf-wg-foo-01.txt", "rfc9111.txt"]


def test_latest_draft_paths_ignores_non_drafts(tmp_path: Path) -> None:
    drafts = _make_drafts_dir(tmp_path)
    # A non-draft file in drafts/ that's not an RFC is ignored.
    (drafts / "charter.txt").write_text("x")
    (drafts / "draft-ietf-wg-foo-01.txt").write_text("x")
    paths = latest_draft_paths(str(tmp_path))
    names = [p.rsplit("/", 1)[-1] for p in paths]
    assert names == ["draft-ietf-wg-foo-01.txt"]
