"""Tests for the `draft_authors` MCP tool (`tool_draft_authors`).

The author *parser* is covered in `test_draft_authors.py`; here we test the
tool that locates the newest cached revision across corpora and renders it.
The corpus-scan seams (`_list_wgs` / `_files_dir`) are monkeypatched so the
test needs no corpus-store setup — just a drafts dir on disk.
"""

from __future__ import annotations
from ietf_llm import mcp

import os

import pytest


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
def _seeded(tmp_path, monkeypatch):
    """A cache with two revisions of one draft; -02 is newest."""
    ddir = tmp_path / "files" / "drafts"
    ddir.mkdir(parents=True)
    (ddir / "draft-ietf-xtest-foo-01.txt").write_text("old rev\n", encoding="utf-8")
    (ddir / "draft-ietf-xtest-foo-02.txt").write_text(_DRAFT, encoding="utf-8")
    monkeypatch.setattr(mcp.drafts, "_list_wgs", lambda: ["xtest"])
    monkeypatch.setattr(mcp.drafts, "_files_dir", lambda wg: str(tmp_path / "files"))
    return tmp_path


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


def test_find_latest_draft_file_none_when_absent(tmp_path, monkeypatch):
    (tmp_path / "files" / "drafts").mkdir(parents=True)
    monkeypatch.setattr(mcp.drafts, "_list_wgs", lambda: ["xtest"])
    monkeypatch.setattr(mcp.drafts, "_files_dir", lambda wg: str(tmp_path / "files"))
    assert mcp.drafts._find_latest_draft_file("draft-ietf-xtest-foo") is None
    assert os.path.isdir(tmp_path / "files" / "drafts")
