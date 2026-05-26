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


def test_chunk_file_dispatches_by_filename(isolated_home: Path) -> None:
    files_dir = isolated_home / "files"
    files_dir.mkdir()

    ml = files_dir / "wg-mailing-list-2025.txt"
    ml.write_text(_ml_text([
        ("Mon, 01 Jan 2025 10:00:00 +0000", "A <a@x>", "S1", "B1"),
    ]))
    chunks_ml = _chunk_file(str(ml))
    assert len(chunks_ml) == 1
    assert "S1" in chunks_ml[0].title

    gh = files_dir / "wg-github-org-repo.txt"
    gh.write_text(
        "Repository: org/repo\n" + "=" * 80 + "\n\n"
        + "Issue #1: Title here\n\nbody\n\n" + "=" * 80 + "\n"
    )
    chunks_gh = _chunk_file(str(gh))
    assert any("#1: Title here" in c.title for c in chunks_gh)

    draft = files_dir / "draft-foo-00.txt"
    draft.write_text("Some draft content.\n" + ("x " * 100))
    chunks_d = _chunk_file(str(draft))
    assert len(chunks_d) == 1
    assert chunks_d[0].title.startswith("Some draft content")


# --- eligibility filter ----------------------------------------------------


def test_eligible_files_includes_txt_and_md(isolated_home: Path) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    for name in ["wg-charter.txt", "ietf124-minutes.md", "draft-foo-00.txt"]:
        (files_dir / name).write_text("x")
    eligible = _eligible_files(str(files_dir), "wg")
    bases = [os.path.basename(p) for p in eligible]
    assert sorted(bases) == ["draft-foo-00.txt", "ietf124-minutes.md", "wg-charter.txt"]


def test_eligible_files_excludes_digests_json_and_pdf(isolated_home: Path) -> None:
    files_dir = isolated_home / ".cache" / "ietf-llm" / "wg" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "wg-_index.md").write_text("# digest")
    (files_dir / "wg-_issues.md").write_text("# digest")
    (files_dir / "wg-github-x.json").write_text("{}")
    (files_dir / "wg-slides.pdf").write_bytes(b"pdf")
    (files_dir / "wg-charter.txt").write_text("real")
    eligible = _eligible_files(str(files_dir), "wg")
    bases = [os.path.basename(p) for p in eligible]
    assert bases == ["wg-charter.txt"]
