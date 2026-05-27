"""Tests for the corpus index digest (`digests/index.md`).

Post-reorg, `_inventory` walks the cache recursively and buckets files
by their directory placement under the WG cache.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest import _inventory

from conftest import write_cache_file


def test_inventory_categorises_files(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "charter.txt")
    write_cache_file(isolated_home, "wg", "drafts/draft-ietf-wg-foo-01.txt")
    write_cache_file(isolated_home, "wg", "drafts/rfc1234.txt")
    write_cache_file(isolated_home, "wg", "meetings/ietf124/minutes.md")
    # PDFs are deliberately excluded from the index (slides .pdf.txt
    # carries the text content; the .pdf itself is binary).
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf124/slides/foo-00.pdf.txt", "slide text",
    )
    write_cache_file(
        isolated_home, "wg",
        "meetings/ietf124/transcripts/202511052100.md", "WEBVTT\n",
    )
    write_cache_file(isolated_home, "wg", "raw/mail-archive-2025.txt")
    write_cache_file(isolated_home, "wg", "raw/github-org-repo.txt")

    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir))

    flat = [name for bucket in buckets.values() for name, _size in bucket]
    assert "charter.txt" in flat
    assert "drafts/draft-ietf-wg-foo-01.txt" in flat
    assert "drafts/rfc1234.txt" in flat
    assert "meetings/ietf124/minutes.md" in flat
    assert "meetings/ietf124/slides/foo-00.pdf.txt" in flat
    assert "meetings/ietf124/transcripts/202511052100.md" in flat
    assert "raw/mail-archive-2025.txt" in flat
    assert "raw/github-org-repo.txt" in flat


def test_inventory_excludes_internal_json(isolated_home: Path) -> None:
    # The raw github archive JSON lives under github/ and must NOT
    # appear in the index (it's internal data, not corpus).
    write_cache_file(isolated_home, "wg", "github/org-repo.json", "{}")
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir))
    flat = [name for bucket in buckets.values() for name, _size in bucket]
    assert all(not n.endswith(".json") for n in flat)


def test_inventory_excludes_digest_files(isolated_home: Path) -> None:
    # Digests are the index itself; they shouldn't be listed as corpus
    # contents on the index page.
    write_cache_file(isolated_home, "wg", "digests/index.md", "# old index")
    write_cache_file(isolated_home, "wg", "digests/issues.md", "# old issues")
    write_cache_file(isolated_home, "wg", "digests/threads.md", "# old threads")
    write_cache_file(isolated_home, "wg", "charter.txt")
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    buckets = _inventory(str(files_dir))
    flat = [name for bucket in buckets.values() for name, _size in bucket]
    assert "charter.txt" in flat
    assert not any(n.startswith("digests/") for n in flat)


def test_inventory_handles_empty_cache(isolated_home: Path) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    buckets = _inventory(str(files_dir))
    assert all(v == [] for v in buckets.values())
