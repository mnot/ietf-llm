"""End-to-end tests for the mailing list threads digest.

Covers:
- IMAP cache path layout matches what mbox.py writes (the regression).
- Subject normalisation actually groups Re:/Fwd:/[wg] variants.
- Mixed tz-aware/naive Date headers don't crash thread building.
- Sort order: most recently active threads first.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.digest import generate_digests
from ietf_llm.utils import get_wg_file_cache_dir

from conftest import write_eml


def _digest_text(wg: str) -> str:
    path = Path(get_wg_file_cache_dir(wg)) / "digests" / "threads.md"
    return path.read_text()


def test_no_imap_cache_no_digest(isolated_home: Path) -> None:
    paths = generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    assert not (Path(get_wg_file_cache_dir("wg")) / "digests/threads.md").exists()
    assert all("digests/threads.md" not in p for p in paths)


def test_imap_cache_at_correct_path_produces_digest(isolated_home: Path) -> None:
    # The regression bug had digest.py looking at the wrong path
    # (<wg>/imap-cache/ instead of imap-cache/<wg>/). This test pins
    # the correct location.
    write_eml(
        isolated_home, "wg", "ai-control", 1,
        "Cookie partitioning", "Alice <a@x>",
        "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "Cookie partitioning" in text


def test_subject_variants_collapse_to_one_thread(isolated_home: Path) -> None:
    write_eml(isolated_home, "wg", "list", 1,
              "Cookie partitioning", "Alice <a@x>",
              "Mon, 01 Jan 2025 10:00:00 +0000")
    write_eml(isolated_home, "wg", "list", 2,
              "Re: [wg] Cookie partitioning", "Bob <b@x>",
              "Tue, 02 Jan 2025 10:00:00 +0000")
    write_eml(isolated_home, "wg", "list", 3,
              "Re: Re: Cookie partitioning", "Carol <c@x>",
              "Wed, 03 Jan 2025 10:00:00 +0000")
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    # 1 thread, 3 messages, 3 participants
    assert "_3 threads" not in text  # not three separate threads
    assert "1 threads across 3 messages" in text


def test_mixed_aware_and_naive_dates_do_not_crash(isolated_home: Path) -> None:
    # Pre-fix this would TypeError on the comparison `date < thread['first']`.
    write_eml(isolated_home, "wg", "list", 1,
              "Topic A", "Alice <a@x>",
              "Mon, 01 Jan 2025 10:00:00 +0000")        # aware
    write_eml(isolated_home, "wg", "list", 2,
              "Re: Topic A", "Bob <b@x>",
              "Tue, 02 Jan 2025 10:00:00")              # naive
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "1 threads across 2 messages" in text


def test_threads_sorted_by_last_activity_desc(isolated_home: Path) -> None:
    write_eml(isolated_home, "wg", "list", 1, "Old topic", "Alice <a@x>",
              "Mon, 01 Jan 2025 10:00:00 +0000")
    write_eml(isolated_home, "wg", "list", 2, "New topic", "Bob <b@x>",
              "Mon, 01 Jun 2026 10:00:00 +0000")
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    new_pos = text.find("New topic")
    old_pos = text.find("Old topic")
    assert 0 < new_pos < old_pos


def test_threads_digest_roundtrips_through_query_digest(
    isolated_home: Path,
) -> None:
    # Writer -> reader round-trip. The real threads digest, fed back
    # through query_digest, must honour limit / since with the header
    # appearing exactly once (no duplicated unfiltered copy). This is
    # the structural guard for the class of bug where the reader's
    # preamble logic diverges from the shape the writer emits: the
    # threads writer puts one table directly under the `# ` title with
    # no `## ` heading, which earlier query_digest fixtures never did.
    from ietf_llm.digest.query import query_digest

    threads = [
        ("Topic A", "Mon, 02 Mar 2026 10:00:00 +0000"),
        ("Topic B", "Mon, 09 Mar 2026 10:00:00 +0000"),
        ("Topic C", "Mon, 16 Mar 2026 10:00:00 +0000"),
        ("Topic D", "Mon, 04 May 2026 10:00:00 +0000"),
    ]
    for i, (subject, when) in enumerate(threads, start=1):
        write_eml(isolated_home, "wg", "list", i, subject, "Alice <a@x>", when)
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    path = str(Path(get_wg_file_cache_dir("wg")) / "digests" / "threads.md")

    def data_rows(md: str) -> list[str]:
        return [
            ln
            for ln in md.splitlines()
            if ln.startswith("| ")
            and "Subject" not in ln
            and set(ln.strip()) - set("|-: ")
        ]

    limited = query_digest(path, "threads", limit=2)
    assert len(data_rows(limited)) == 2  # limit truncates, not all four
    assert limited.count("| Subject |") == 1  # header not duplicated

    recent = query_digest(path, "threads", since="2026-04-01")
    assert "Topic D" in recent  # May activity kept
    assert "Topic A" not in recent  # March activity dropped
    assert recent.count("| Subject |") == 1


def test_malformed_eml_is_skipped_not_fatal(isolated_home: Path) -> None:
    # A garbage .eml in the IMAP cache must not abort the digest; the
    # narrowed except in _build_threads_digest catches the malformed
    # message and continues to the next one.
    write_eml(
        isolated_home, "wg", "list", 1, "Real subject",
        "Alice <a@x>", "Mon, 01 Jan 2025 10:00:00 +0000",
    )
    # Plant a corrupt eml alongside a good one.
    imap_dir = (
        isolated_home / ".cache" / "ietf-llm" / "imap-cache" / "wg" / "list"
    )
    (imap_dir / "999.eml").write_bytes(b"\x00\x01\x02 not an email at all")
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    # Good message still indexed; bad message reported skipped.
    assert "Real subject" in text


def test_multiple_lists_under_one_wg_are_merged(isolated_home: Path) -> None:
    # A WG could in principle have more than one mailing list; both
    # should be scanned (digest walks the IMAP tree).
    write_eml(isolated_home, "wg", "list-a", 1, "From list A",
              "Alice <a@x>", "Mon, 01 Jan 2025 10:00:00 +0000")
    write_eml(isolated_home, "wg", "list-b", 1, "From list B",
              "Bob <b@x>", "Mon, 01 Jan 2025 10:00:00 +0000")
    generate_digests("wg", get_wg_file_cache_dir("wg"), summarize_model=None)
    text = _digest_text("wg")
    assert "From list A" in text
    assert "From list B" in text
    assert "2 threads across 2 messages" in text
