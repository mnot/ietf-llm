"""Tests for the chunking strategies in ietf_llm.embeddings.

No network, no model: just verifies that:
- Mailing-list files split one chunk per message (===... separator),
  with subject/from/date captured in the title.
- GitHub issue files split one chunk per record (=== separator), with
  '#N: title' as the title when the record matches the issue header.
- Windowed chunking respects CHUNK_SIZE / overlap and produces sensible
  titles.
- _eligible_files includes .txt/.md and excludes internal/digest files.
"""

from __future__ import annotations

import os
from pathlib import Path

from ietf_llm.embeddings import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    _chunk_file,
    _chunk_issues_file,
    _chunk_message_file,
    _chunk_windowed,
    _eligible_files,
)
from ietf_llm.embeddings.chunking import _chunk_thread_file


# --- per-message chunking --------------------------------------------------


def _ml_text(messages: list[tuple[str, str, str, str]]) -> str:
    """Build a synthetic mailing-list-YYYY.txt with === separators."""
    parts = []
    for date, frm, subj, body in messages:
        parts.append(
            f"Date: {date}\nFrom: {frm}\nSubject: {subj}\n\n{body}\n"
        )
    return ("\n" + "=" * 80 + "\n\n").join(parts) + "\n" + "=" * 80 + "\n"


def test_message_chunking_yields_one_chunk_per_message() -> None:
    text = _ml_text([
        ("Mon, 01 Jan 2025 10:00:00 +0000", "Alice <a@x>", "Topic A", "B1"),
        ("Tue, 02 Jan 2025 10:00:00 +0000", "Bob <b@x>", "Re: Topic A", "B2"),
        ("Wed, 03 Jan 2025 10:00:00 +0000", "Carol <c@x>", "Re: Topic A", "B3"),
    ])
    chunks = _chunk_message_file(text, "wg-mailing-list-2025.txt")
    assert len(chunks) == 3
    assert "Topic A" in chunks[0].title
    assert "Alice" in chunks[0].title
    # chunk index is positional within the file
    assert [c.chunk_idx for c in chunks] == [0, 1, 2]


def test_message_chunking_tracks_line_numbers() -> None:
    text = _ml_text([
        ("Mon, 01 Jan 2025 10:00:00 +0000", "Alice <a@x>", "Topic A", "B1"),
        ("Tue, 02 Jan 2025 10:00:00 +0000", "Bob <b@x>", "Topic B", "B2"),
    ])
    chunks = _chunk_message_file(text, "wg-mailing-list-2025.txt")
    # All chunks have populated, monotone, plausible line ranges.
    assert all(c.start_line is not None and c.end_line is not None for c in chunks)
    assert chunks[0].start_line == 1
    # The second message starts strictly after the first one ends.
    assert chunks[1].start_line > chunks[0].end_line  # type: ignore[operator]
    # And the reported end_line of the last chunk doesn't exceed the file.
    file_lines = text.count("\n") + 1
    assert chunks[-1].end_line <= file_lines  # type: ignore[operator]


def test_windowed_chunking_tracks_line_numbers() -> None:
    # Build text where each window's expected line range is predictable.
    lines = [f"line {i}" for i in range(1, 501)]
    text = "\n".join(lines) + "\n"
    chunks = _chunk_windowed(text, "draft-foo-00.txt")
    assert chunks[0].start_line == 1
    # Successive chunks advance in line number (overlap means they
    # overlap, but each new chunk's start_line >= previous start_line).
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_line is not None and prev.start_line is not None
        assert nxt.start_line > prev.start_line


def test_issue_chunking_tracks_line_numbers() -> None:
    text = (
        "Repository: org/repo\n" + "=" * 80 + "\n\n"
        + "Issue #42: First\nState: open\n\nbody\n\n" + "=" * 80 + "\n\n"
        + "Issue #43: Second\nState: closed\n\nbody2\n\n" + "=" * 80 + "\n"
    )
    chunks = _chunk_issues_file(text, "wg-github-org-repo.txt")
    assert all(c.start_line is not None for c in chunks)
    # Header record starts at line 1.
    assert chunks[0].start_line == 1
    # Issue #42 record comes after the first separator.
    assert chunks[1].start_line > chunks[0].end_line  # type: ignore[operator]


def test_message_chunking_handles_missing_headers() -> None:
    text = "Subject: only this\n\nbody\n\n" + "=" * 80 + "\n"
    chunks = _chunk_message_file(text, "wg-mailing-list-2025.txt")
    assert len(chunks) == 1
    assert "only this" in chunks[0].title


# --- per-issue chunking ----------------------------------------------------


def test_issue_chunking_extracts_number_and_title() -> None:
    text = (
        "Repository: org/repo\n" + "=" * 80 + "\n\n"
        + "Issue #42: Cookie partitioning\nState: open\n\nbody\n\n"
        + "=" * 80 + "\n\n"
        + "Issue #43: Editorial nit\nState: closed\n\nbody2\n\n"
        + "=" * 80 + "\n"
    )
    chunks = _chunk_issues_file(text, "wg-github-org-repo.txt")
    # Three records: header + two issues.
    assert len(chunks) == 3
    assert chunks[0].title.startswith("record")  # repo header doesn't match
    assert "#42: Cookie partitioning" in chunks[1].title
    assert "#43: Editorial nit" in chunks[2].title


# --- windowed chunking -----------------------------------------------------


def test_windowed_chunking_respects_size_and_overlap() -> None:
    text = "Title line\n" + ("xxx " * 2000)  # ~8000 chars
    chunks = _chunk_windowed(text, "draft-foo-00.txt")
    assert len(chunks) > 1
    # First chunk should be at most CHUNK_SIZE chars.
    assert len(chunks[0].text) <= CHUNK_SIZE
    # First chunk's title is the first non-empty line.
    assert chunks[0].title.startswith("Title line")
    # Sequential chunks advance by (CHUNK_SIZE - CHUNK_OVERLAP) chars.
    step = CHUNK_SIZE - CHUNK_OVERLAP
    # Window 1 starts at offset `step`; its text must match.
    assert chunks[1].text == text[step : step + CHUNK_SIZE]


def test_windowed_chunking_empty_text() -> None:
    assert _chunk_windowed("", "anything.txt") == []
    assert _chunk_windowed("   \n\n  ", "anything.txt") == []


def test_chunk_file_dispatches_by_relpath(isolated_home: Path) -> None:
    # Post-reorg: dispatch keys off the relative path (under cache_dir),
    # not the basename. Thread files live in `threads/`, issue files
    # in `issues/`, everything else uses the windowed chunker.
    files_dir = isolated_home / "files"
    files_dir.mkdir()
    (files_dir / "threads").mkdir()
    (files_dir / "issues" / "org-repo").mkdir(parents=True)

    thread = files_dir / "threads" / "2025-01-01-topic.md"
    thread.write_text(
        "# Topic\n\n## Messages\n\n"
        "### [1] 2025-01-01 10:00 — Alice\n\nbody\n"
    )
    chunks_t = _chunk_file(str(thread), "threads/2025-01-01-topic.md")
    assert any("Alice" in c.title for c in chunks_t)

    issue = files_dir / "issues" / "org-repo" / "1.md"
    issue.write_text(
        "# Issue #1\n\n**State:** OPEN\n\n## Description\n\n"
        "### [1] 2026-01-01 10:00 — Bob _(opened issue)_\n\nbody\n"
    )
    chunks_i = _chunk_file(str(issue), "issues/org-repo/1.md")
    assert any("Bob" in c.title for c in chunks_i)

    draft = files_dir / "drafts" / "draft-foo-00.txt"
    draft.parent.mkdir(parents=True)
    draft.write_text("Some draft content.\n" + ("x " * 100))
    chunks_d = _chunk_file(str(draft), "drafts/draft-foo-00.txt")
    assert len(chunks_d) == 1
    assert chunks_d[0].title.startswith("Some draft content")


# --- eligibility filter ----------------------------------------------------


def test_eligible_files_includes_txt_and_md(isolated_home: Path) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "charter.txt").write_text("x")
    (files_dir / "meetings" / "ietf124").mkdir(parents=True)
    (files_dir / "meetings" / "ietf124" / "minutes.md").write_text("x")
    (files_dir / "drafts").mkdir()
    (files_dir / "drafts" / "draft-foo-00.txt").write_text("x")
    eligible = _eligible_files(str(files_dir), "wg")
    relpaths = sorted(os.path.relpath(p, str(files_dir)) for p in eligible)
    assert relpaths == [
        "charter.txt",
        "drafts/draft-foo-00.txt",
        "meetings/ietf124/minutes.md",
    ]


def test_eligible_files_excludes_digests_json_pdf_and_raw(
    isolated_home: Path,
) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    # Digests, github archives, PDFs, and raw/ are all excluded.
    (files_dir / "digests").mkdir()
    (files_dir / "digests" / "index.md").write_text("# digest")
    (files_dir / "digests" / "issues.md").write_text("# digest")
    (files_dir / "github").mkdir()
    (files_dir / "github" / "x.json").write_text("{}")
    (files_dir / "raw").mkdir()
    (files_dir / "raw" / "mail-archive-2025.txt").write_text("legacy")
    (files_dir / "meetings" / "ietf124" / "slides").mkdir(parents=True)
    (files_dir / "meetings" / "ietf124" / "slides" / "foo.pdf").write_bytes(b"pdf")
    (files_dir / "charter.txt").write_text("real")
    eligible = _eligible_files(str(files_dir), "wg")
    relpaths = sorted(os.path.relpath(p, str(files_dir)) for p in eligible)
    assert relpaths == ["charter.txt"]


# --- chunker resilience to role-tagged section headers --------------------


def test_thread_chunker_parses_section_header_with_role_tag() -> None:
    # Consumer feedback #6: section headers now carry "(Chair)" /
    # "(Editor)" / etc. The chunker's lazy `(.+?)` must include the
    # role tag in the captured title and still recognise the optional
    # reply-to suffix. Regression test for the regex.
    text = (
        "# Topic\n\n"
        "## Outline\n\n"
        "- **[1]** 2025-01-01 10:00 — Mark Nottingham (Chair)\n"
        "- **[2]** 2025-01-02 10:00 — Martin Thomson (Editor)\n\n"
        "## Messages\n\n"
        "### [1] 2025-01-01 10:00 — Mark Nottingham (Chair)\n\nbody one\n\n"
        "### [2] 2025-01-02 10:00 — Martin Thomson (Editor) (reply to [1])\n\n"
        "body two\n"
    )
    chunks = _chunk_thread_file(text, "wg-thread-2025-01-01-topic.md")
    # Header chunk + two message chunks.
    assert len(chunks) == 3
    # The role tag survives into the chunk title — so search hits will
    # surface it visibly in the title line. This is the load-bearing
    # property: a regex change that swallowed "(Chair)" / "(Editor)"
    # into the optional reply-to group would silently lose role
    # attribution from every indexed chunk.
    assert "Mark Nottingham (Chair)" in chunks[1].title
    assert "Martin Thomson (Editor)" in chunks[2].title
    # Date extraction (group 2 in the regex) still works — the chunk
    # carries a chunk_date derived from the header timestamp.
    assert chunks[1].chunk_date is not None
    assert chunks[2].chunk_date is not None
