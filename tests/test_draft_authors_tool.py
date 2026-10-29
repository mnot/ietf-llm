"""Tests for the `draft_authors` MCP tool (`tool_draft_authors`).

The author *parser* is covered in `test_draft_authors.py`; here we test the
tool that locates the newest cached revision across corpora and renders it.
The corpus is seeded in the sandboxed local cache, so the locator runs against
the real store.
"""

from __future__ import annotations
from ietf_llm import mcp

from pathlib import Path

import pytest

from conftest import write_cache_file


_DRAFT = """\
Internet-Draft                                              Example Draft
Intended status: Standards Track

Body of the draft goes here.

Authors' Addresses

   Alice Example (editor)
   Example Corp
   Email: alice@example.com

   Bob Builder
   Email: bob@builder.example
"""


@pytest.fixture
def _seeded(isolated_home: Path) -> Path:
    """A cache with two revisions of one draft; -02 is newest."""
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-foo-01.txt", "old rev\n")
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-foo-02.txt", _DRAFT)
    return isolated_home


def test_draft_authors_picks_newest_revision_and_roles(_seeded):
    out = mcp.drafts.tool_draft_authors("draft-ietf-xtest-foo")
    assert "draft-ietf-xtest-foo-02.txt" in out  # newest, not -01
    assert "**Alice Example** (editor), Example Corp — alice@example.com" in out
    assert "**Bob Builder** (author) — bob@builder.example" in out


def test_draft_authors_accepts_versioned_name(_seeded):
    out = mcp.drafts.tool_draft_authors("draft-ietf-xtest-foo-01")
    # The version suffix is stripped; still resolves to the newest (-02).
    assert "draft-ietf-xtest-foo-02.txt" in out


def test_draft_authors_unknown_draft(monkeypatch):
    monkeypatch.setattr(mcp.drafts, "_list_wgs", lambda: [])
    out = mcp.drafts.tool_draft_authors("draft-ietf-xtest-missing")
    assert "No cached copy" in out


def test_draft_authors_empty_name():
    assert "Provide a draft name" in mcp.drafts.tool_draft_authors("")


def test_find_latest_draft_file_none_when_absent(isolated_home):
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-bar-00.txt")
    assert mcp.drafts._find_latest_draft_file("draft-ietf-xtest-foo") is None


def test_revision_beats_a_lexically_larger_name(isolated_home):
    """-10 is newer than -9 (an int compare, not a string one)."""
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-foo-9.txt")
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-foo-10.txt")
    path = mcp.drafts._find_latest_draft_file("draft-ietf-xtest-foo")
    assert path is not None and path.endswith("draft-ietf-xtest-foo-10.txt")


def test_draft_held_by_a_non_owning_corpus_is_still_found(isolated_home):
    """The name implies a WG, but the draft may be cached only under another
    corpus (an individual-draft or topic corpus)."""
    write_cache_file(isolated_home, "xtest", "drafts/draft-ietf-xtest-other-00.txt")
    write_cache_file(isolated_home, "xother", "drafts/draft-ietf-xtest-foo-03.txt")
    path = mcp.drafts._find_latest_draft_file("draft-ietf-xtest-foo")
    assert path is not None
    assert path.endswith("xother/files/drafts/draft-ietf-xtest-foo-03.txt")


def test_individual_draft_has_no_owner_but_is_found(isolated_home):
    write_cache_file(isolated_home, "xtest", "drafts/draft-someone-foo-01.txt")
    assert mcp.drafts._find_latest_draft_file("draft-someone-foo") is not None


def test_owner_candidates_handle_hyphenated_acronyms():
    owners = mcp.drafts._owner_candidates
    corpora = ["dns-sd", "dns", "tls"]
    assert owners("draft-ietf-dns-sd-foo", corpora) == ["dns-sd", "dns"]
    assert owners("draft-irtf-tls-foo", corpora) == ["tls"]
    assert owners("draft-someone-tls-foo", corpora) == []
    # The draft's own name keeps at least one token, so a corpus named for it
    # is not mistaken for the owner.
    assert owners("draft-ietf-tls", corpora) == []
