"""Tests for the corpus index digest (_build_index → <wg>-_index.md).

Verifies the file-categorisation logic that decides which files belong
to which bucket (charter, drafts, RFCs, meetings, transcripts, mailing
list, github, other).
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest import _inventory

from conftest import write_cache_file


def test_inventory_categorises_files(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "wg-charter.txt")
    write_cache_file(isolated_home, "wg", "draft-ietf-wg-foo-01.txt")
    write_cache_file(isolated_home, "wg", "rfc1234.txt")
    write_cache_file(isolated_home, "wg", "ietf124-minutes.md")
    write_cache_file(isolated_home, "wg", "ietf124-slides.pdf")
    write_cache_file(isolated_home, "wg", "ietf-wg-transcript.md")
    write_cache_file(isolated_home, "wg", "wg-mailing-list-2025.txt")
    write_cache_file(isolated_home, "wg", "wg-github-org-repo.txt")
    write_cache_file(isolated_home, "wg", "wg-mystery-file.txt")

    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir), "wg")

    assert "wg-charter.txt" in buckets["charter"]
    assert "draft-ietf-wg-foo-01.txt" in buckets["drafts"]
    assert "rfc1234.txt" in buckets["rfcs"]
    assert "ietf124-minutes.md" in buckets["meetings"]
    assert "ietf124-slides.pdf" in buckets["meetings"]
    assert "ietf-wg-transcript.md" in buckets["transcripts"]
    assert "wg-mailing-list-2025.txt" in buckets["mailing_list"]
    assert "wg-github-org-repo.txt" in buckets["github"]
    assert "wg-mystery-file.txt" in buckets["other"]


def test_inventory_excludes_internal_json(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "wg-github-org-repo.json")
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir), "wg")
    flat = [n for bucket in buckets.values() for n in bucket]
    assert all(not n.endswith(".json") for n in flat)


def test_inventory_excludes_digest_files(isolated_home: Path) -> None:
    # Files named "<wg>-_*.md" are the digest outputs themselves and
    # shouldn't be listed back as corpus contents.
    write_cache_file(isolated_home, "wg", "wg-_index.md", "# old index")
    write_cache_file(isolated_home, "wg", "wg-_issues.md", "# old issues")
    write_cache_file(isolated_home, "wg", "wg-_threads.md", "# old threads")
    write_cache_file(isolated_home, "wg", "wg-charter.txt")
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir), "wg")
    flat = [n for bucket in buckets.values() for n in bucket]
    assert "wg-charter.txt" in flat
    assert not any(n.startswith("wg-_") for n in flat)


def test_inventory_handles_empty_cache(isolated_home: Path) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    buckets = _inventory(str(files_dir), "wg")
    assert all(v == [] for v in buckets.values())
