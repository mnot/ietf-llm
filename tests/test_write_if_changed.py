"""Tests for write_if_changed line-ending normalisation.

Regression: source data with CRLF (GitHub comment bodies, RFC 5322
mail) caused a file to be rewritten on every gather. The file stored
CRLF, but reading it back in text mode translated CRLF→LF, so a naive
`read() == content` never matched and the file churned forever (and,
for embedded files like threads, re-embedded). write_if_changed now
normalises to LF and compares by bytes, so CRLF content is idempotent.
"""

from __future__ import annotations

from pathlib import Path

from ietf_llm.atomicio import write_if_changed


def test_crlf_content_is_written_once_then_stable(tmp_path: Path) -> None:
    p = str(tmp_path / "issue.md")
    crlf = "line one\r\nline two\r\n"

    assert write_if_changed(p, crlf) is True          # first write
    # Stored as LF, single-newline corpus.
    assert (tmp_path / "issue.md").read_bytes() == b"line one\nline two\n"
    # Re-writing the same CRLF content is a no-op — the bug was this
    # returning True forever.
    assert write_if_changed(p, crlf) is False
    assert write_if_changed(p, crlf) is False


def test_lone_cr_normalised(tmp_path: Path) -> None:
    p = str(tmp_path / "x.md")
    assert write_if_changed(p, "a\rb\r") is True
    assert (tmp_path / "x.md").read_bytes() == b"a\nb\n"
    assert write_if_changed(p, "a\rb\r") is False


def test_real_change_still_writes(tmp_path: Path) -> None:
    p = str(tmp_path / "x.md")
    assert write_if_changed(p, "hello\n") is True
    assert write_if_changed(p, "hello\n") is False
    assert write_if_changed(p, "goodbye\n") is True
