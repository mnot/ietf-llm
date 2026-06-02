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
    EMBED_CHAR_BUDGET,
    EMBED_CHAR_OVERLAP,
    _chunk_file,
    _chunk_issues_file,
    _chunk_message_file,
    _chunk_windowed,
    _eligible_files,
    _window_text,
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
    # Every window fits the embedding budget.
    assert all(len(c.text) <= EMBED_CHAR_BUDGET for c in chunks)
    # First chunk's title is the first non-empty line.
    assert chunks[0].title.startswith("Title line")
    # Sequential chunks advance by (budget - overlap) chars.
    step = EMBED_CHAR_BUDGET - EMBED_CHAR_OVERLAP
    stripped = text.strip()
    # Window 1 starts at offset `step`; its text must match.
    assert chunks[1].text == stripped[step : step + EMBED_CHAR_BUDGET]
    # Windows are each their own chunk_idx with sub_idx 0 (no sub-splitting).
    assert [c.chunk_idx for c in chunks] == list(range(len(chunks)))
    assert all(c.sub_idx == 0 for c in chunks)


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
    chunks = _chunk_thread_file(text, "threads/2025-01-01-topic.md")
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


# --- token-budget windowing of long sections ------------------------------


def _embedded(chunk) -> str:  # type: ignore[no-untyped-def]
    """The text actually fed to the embedding model for a chunk."""
    return chunk.embed_text if chunk.embed_text is not None else chunk.text


def test_window_text_covers_input_with_fixed_overlap() -> None:
    text = "".join(f"{i:04d}-" for i in range(1000))  # 5000 chars, no newlines
    windows = _window_text(text, 1000, 100)
    assert len(windows) > 1
    # Offsets are exact slices of the input.
    for sub, start, end in windows:
        assert text[start:end] == sub
        assert len(sub) <= 1000
    # Consecutive windows overlap by exactly `overlap` chars.
    for (s0, _a0, e0), (s1, a1, _e1) in zip(windows, windows[1:]):
        assert e0 - a1 == 100
    # Reconstruction modulo overlap == the original.
    rebuilt = windows[0][0] + "".join(s[100:] for s, _a, _e in windows[1:])
    assert rebuilt == text


def test_no_embedded_fragment_exceeds_budget() -> None:
    # A message section far over budget must split so that every embedded
    # fragment fits — nothing is silently dropped at embed time.
    long_body = "word " * 1200  # ~6000 chars
    text = (
        "# Topic\n\n## Messages\n\n"
        f"### [1] 2025-01-01 10:00 — Alice\n\n{long_body}\n"
    )
    chunks = _chunk_thread_file(text, "threads/2025-01-01-topic.md")
    assert all(len(_embedded(c)) <= EMBED_CHAR_BUDGET for c in chunks)


def test_long_section_splits_into_covering_fragments_sharing_metadata() -> None:
    # An over-budget issue comment splits into several sub_idx fragments
    # that (a) all share the same chunk_idx and section metadata, and
    # (b) together cover the whole comment.
    long_body = "alpha bravo charlie delta echo " * 120  # ~3700 chars
    text = (
        "# Issue #7: Long one\n\n"
        "**State:** OPEN  \n"
        "**Labels:** vocabulary, top-level  \n"
        "**URL:** https://github.com/org/repo/issues/7  \n\n"
        "## Description\n\n"
        f"### [1] 2026-01-01 10:00 — Bob _(opened issue)_\n\n{long_body}\n"
    )
    chunks = _chunk_thread_file(text, "issues/org-repo/7.md")
    msg_frags = [c for c in chunks if c.chunk_idx == 1]
    assert len(msg_frags) > 1  # actually split
    # All fragments share chunk_idx, metadata, and sub_idx 0..n-1.
    assert [c.sub_idx for c in msg_frags] == list(range(len(msg_frags)))
    for c in msg_frags:
        assert c.chunk_date == "2026-01-01T10:00:00Z"
        assert c.labels == "vocabulary,top-level"
        assert c.state == "open"
        assert c.url == "https://github.com/org/repo/issues/7"
        assert "(part" in c.title  # legibility hint on split fragments
    # sub_idx 0 carries the FULL body (retrieval), later frags their slice.
    assert long_body.strip() in msg_frags[0].text
    # Every embedded fragment fits the budget.
    assert all(len(_embedded(c)) <= EMBED_CHAR_BUDGET for c in msg_frags)
    # The embedded fragments together cover the whole body (modulo overlap):
    # concatenating their embedded text with overlap removed reproduces it.
    embedded = [_embedded(c) for c in msg_frags]
    rebuilt = embedded[0] + "".join(e[EMBED_CHAR_OVERLAP:] for e in embedded[1:])
    assert msg_frags[0].text == rebuilt  # full body == reconstruction


def test_sub_fragment_line_ranges_are_contiguous_and_correct() -> None:
    # Build a section whose body is many short lines so line mapping is
    # easy to reason about, large enough to force several fragments.
    body_lines = [f"line {i:03d} of the body content here" for i in range(200)]
    body = "\n".join(body_lines)
    text = (
        "# Topic\n\n## Messages\n\n"
        f"### [1] 2025-01-01 10:00 — Alice\n\n{body}\n"
    )
    chunks = _chunk_thread_file(text, "threads/2025-01-01-topic.md")
    frags = [c for c in chunks if c.chunk_idx == 1]
    assert len(frags) > 1
    # sub_idx 0 spans the whole section; its end_line is the last body line.
    assert frags[0].start_line is not None and frags[0].end_line is not None
    # Later fragments stay within the section and advance monotonically,
    # with the next fragment starting at or before the previous one's end
    # (overlap) — so the union covers every line with no gaps.
    for prev, nxt in zip(frags[1:], frags[2:]):
        assert nxt.start_line is not None and prev.start_line is not None
        assert nxt.start_line >= prev.start_line
        assert nxt.start_line <= prev.end_line + 1  # type: ignore[operator]
    # No fragment's line range exceeds sub_idx 0's full-section span.
    for c in frags[1:]:
        assert c.start_line >= frags[0].start_line  # type: ignore[operator]
        assert c.end_line <= frags[0].end_line  # type: ignore[operator]
