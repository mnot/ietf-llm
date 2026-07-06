"""Tests for PDF text extraction.

A minimal hand-crafted PDF (embedded as bytes below) keeps the round-
trip test dependency-free: we don't pull in reportlab just to make
a fixture. The MINIMAL_PDF says "Hello AIPREF" on one page; that's
the string we assert on.
"""

from __future__ import annotations

import time
from pathlib import Path

import ietf_llm.gather.sources.pdf_extract as pdf_extract
from ietf_llm.gather.sources.pdf_extract import (
    _strip_surrogates,
    extract_all_pdfs,
    extract_pdf_text,
    slide_context,
)


# A valid 1-page PDF that renders "Hello AIPREF" using Helvetica.
# Hand-built so the test has zero external fixture dependencies.
MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n"
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    b"4 0 obj\n<< /Length 44 >>\nstream\n"
    b"BT /F1 24 Tf 100 700 Td (Hello AIPREF) Tj ET\n"
    b"endstream\nendobj\n"
    b"5 0 obj\n"
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000056 00000 n \n"
    b"0000000111 00000 n \n"
    b"0000000212 00000 n \n"
    b"0000000301 00000 n \n"
    b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
    b"startxref\n367\n%%EOF\n"
)


def test_extract_pdf_text_returns_content(tmp_path: Path) -> None:
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    text = extract_pdf_text(str(pdf))
    assert "Hello AIPREF" in text
    # The page marker should be present.
    assert "## Page 1" in text


def test_extract_pdf_text_returns_empty_for_garbage(tmp_path: Path) -> None:
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"this is not a pdf at all")
    assert extract_pdf_text(str(pdf)) == ""


def test_extract_pdf_text_returns_empty_for_missing(tmp_path: Path) -> None:
    assert extract_pdf_text(str(tmp_path / "nope.pdf")) == ""


def test_extract_all_pdfs_writes_sibling_txt(tmp_path: Path) -> None:
    (tmp_path / "slides.pdf").write_bytes(MINIMAL_PDF)
    written = extract_all_pdfs(str(tmp_path))
    assert len(written) == 1
    out_path = tmp_path / "slides.pdf.txt"
    assert out_path.exists()
    assert "Hello AIPREF" in out_path.read_text()


def test_suppress_pdf_drops_source_after_extraction(tmp_path: Path) -> None:
    # A freshly-extracted deck: the .pdf.txt is written, the .pdf dropped.
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    written = extract_all_pdfs(str(tmp_path), suppress_pdf=True)
    txt = tmp_path / "slides.pdf.txt"
    assert txt.exists()
    assert "Hello AIPREF" in txt.read_text()
    assert not pdf.exists()
    assert str(txt) in written


def test_suppress_pdf_sweeps_preexisting_pdf(tmp_path: Path) -> None:
    # A cache gathered before this change: the .pdf.txt is already up to
    # date so _needs_extraction skips re-extraction — the sweep still
    # removes the now-dead .pdf.
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    extract_all_pdfs(str(tmp_path))  # non-suppressed: keeps the .pdf
    assert pdf.exists()
    extract_all_pdfs(str(tmp_path), suppress_pdf=True)
    assert not pdf.exists()
    assert (tmp_path / "slides.pdf.txt").exists()


def test_suppress_pdf_drops_unextractable_source(tmp_path: Path) -> None:
    # An image-only / unextractable deck still gets a stub .txt, and its
    # .pdf is dropped when suppressing.
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"\x00\x01 not a pdf")
    extract_all_pdfs(str(tmp_path), suppress_pdf=True)
    assert (tmp_path / "bad.pdf.txt").exists()
    assert not pdf.exists()


def test_no_suppress_keeps_pdf(tmp_path: Path) -> None:
    # Default (no suppression): the .pdf is kept alongside the .txt.
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    extract_all_pdfs(str(tmp_path))
    assert pdf.exists()
    assert (tmp_path / "slides.pdf.txt").exists()


def test_extract_all_pdfs_skips_when_txt_is_newer(tmp_path: Path) -> None:
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    extract_all_pdfs(str(tmp_path))  # first run
    txt = tmp_path / "slides.pdf.txt"
    first_mtime = txt.stat().st_mtime
    # Ensure the second run's mtime would differ if it ran.
    time.sleep(0.05)
    extract_all_pdfs(str(tmp_path))
    assert txt.stat().st_mtime == first_mtime


def test_extract_all_pdfs_re_extracts_when_pdf_is_newer(tmp_path: Path) -> None:
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    extract_all_pdfs(str(tmp_path))
    txt = tmp_path / "slides.pdf.txt"
    original_mtime = txt.stat().st_mtime
    # Make the .pdf appear newer than the .txt.
    import os  # pylint: disable=import-outside-toplevel

    os.utime(pdf, (time.time() + 60, time.time() + 60))
    time.sleep(0.05)
    extract_all_pdfs(str(tmp_path))
    # The .txt was rewritten — its mtime should have advanced.
    assert txt.stat().st_mtime > original_mtime


def test_extract_all_pdfs_writes_stub_for_unextractable(tmp_path: Path) -> None:
    # An image-only PDF (or a PDF with no extractable text) should
    # still get a sibling .txt — with a stub message — so we don't
    # retry on every gather.
    (tmp_path / "bad.pdf").write_bytes(b"\x00\x01\x02 not a pdf")
    written = extract_all_pdfs(str(tmp_path))
    assert len(written) == 1
    txt = tmp_path / "bad.pdf.txt"
    assert txt.exists()
    assert "No extractable text" in txt.read_text()


def test_strip_surrogates_replaces_lone_surrogates() -> None:
    # A lone high surrogate that would raise UnicodeEncodeError on a
    # strict utf-8 write becomes U+FFFD; surrounding text is untouched.
    cleaned = _strip_surrogates("a\ud834bc\udfff")
    assert cleaned == "a�bc�"
    # And the result actually encodes as utf-8 now.
    assert cleaned.encode("utf-8")


def test_extract_all_pdfs_survives_unencodable_text(
    tmp_path: Path, monkeypatch
) -> None:
    # A deck whose extracted text still can't be utf-8 encoded (a lone
    # surrogate that slipped past sanitisation) must not abort the gather:
    # it degrades to a logged skip, and other decks still get written.
    good = tmp_path / "good.pdf"
    good.write_bytes(MINIMAL_PDF)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(MINIMAL_PDF)

    def fake_extract(pdf_path: str) -> str:
        if pdf_path.endswith("bad.pdf"):
            return "## Page 1\n\nlone surrogate \ud800 here\n"
        return extract_pdf_text(pdf_path)

    monkeypatch.setattr(pdf_extract, "extract_pdf_text", fake_extract)
    # Should not raise.
    written = extract_all_pdfs(str(tmp_path))
    # The good deck still produced a .txt; the bad one did not.
    assert (tmp_path / "good.pdf.txt").exists()
    assert not (tmp_path / "bad.pdf.txt").exists()
    assert str(tmp_path / "good.pdf.txt") in written


def test_extract_all_pdfs_ignores_non_pdfs(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a pdf")
    (tmp_path / "data.json").write_text("{}")
    written = extract_all_pdfs(str(tmp_path))
    assert written == []


def test_extract_all_pdfs_handles_empty_cache(tmp_path: Path) -> None:
    assert extract_all_pdfs(str(tmp_path / "nope")) == []


# --- Slide context inference ---------------------------------------------


def _seed_meeting(cache: Path, code: str, date_line: str) -> None:
    """Create `meetings/<code>/minutes.md` with a Date: header and
    the empty slides/ subdir ready for slide PDFs."""
    d = cache / "meetings" / code
    (d / "slides").mkdir(parents=True, exist_ok=True)
    (d / "minutes.md").write_text(f"# header\n{date_line}\n")


def test_slide_context_ietf_meeting(tmp_path: Path) -> None:
    _seed_meeting(tmp_path, "ietf124", "Date: 2025-11-05 21:00")
    # Post-reorg: `meetings/<code>/slides/<basename>.pdf`. The meeting
    # code is read from the path, not the filename.
    relpath = (
        "meetings/ietf124/slides/"
        "slides-124-aipref-vocabulary-status-update-01.pdf"
    )
    ctx = slide_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.meeting == "ietf124"
    assert ctx.label == "IETF 124 meeting"
    assert ctx.topic_slug == "vocabulary-status-update"
    assert ctx.version == "01"
    assert ctx.date == "2025-11-05"
    assert ctx.minutes_file == "meetings/ietf124/minutes.md"


def test_slide_context_interim(tmp_path: Path) -> None:
    _seed_meeting(
        tmp_path, "interim2025aipref08", "Date: 2025-06-23 13:00",
    )
    relpath = (
        "meetings/interim2025aipref08/slides/"
        "slides-interim-2025-aipref-08-sessa-draft-status-update-00.pdf"
    )
    ctx = slide_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.meeting == "interim2025aipref08"
    assert ctx.label == "Interim 2025 #08"
    # The session prefix gets stripped.
    assert ctx.topic_slug == "draft-status-update"
    assert ctx.date == "2025-06-23"


def test_slide_context_returns_none_for_non_slide_pdf(tmp_path: Path) -> None:
    # Path not under meetings/<code>/slides/ → no context inferable.
    assert slide_context("drafts/draft-foo-00.pdf", str(tmp_path)) is None
    assert slide_context("random.pdf", str(tmp_path)) is None


def test_slide_context_handles_missing_minutes(tmp_path: Path) -> None:
    # The meeting dir exists but minutes.md doesn't — code is still
    # extracted from the path.
    (tmp_path / "meetings" / "ietf124" / "slides").mkdir(parents=True)
    relpath = "meetings/ietf124/slides/slides-124-aipref-overview-00.pdf"
    ctx = slide_context(relpath, str(tmp_path))
    assert ctx is not None
    assert ctx.label == "IETF 124 meeting"
    assert ctx.date is None
    assert ctx.minutes_file is None


def test_extracted_txt_includes_meeting_context_header(tmp_path: Path) -> None:
    _seed_meeting(tmp_path, "ietf124", "Date: 2025-11-05 21:00")
    pdf_path = (
        tmp_path / "meetings" / "ietf124" / "slides"
        / "slides-124-aipref-overview-00.pdf"
    )
    pdf_path.write_bytes(MINIMAL_PDF)
    extract_all_pdfs(str(tmp_path))
    txt = pdf_path.with_suffix(".pdf.txt").read_text()
    assert "**Meeting:** IETF 124 meeting (2025-11-05)" in txt
    assert "**Topic slug:** `overview`" in txt
    assert "**Minutes:** `meetings/ietf124/minutes.md`" in txt
    # The actual slide content still follows.
    assert "Hello AIPREF" in txt


def test_stubbed_unextractable_still_carries_context(tmp_path: Path) -> None:
    _seed_meeting(tmp_path, "ietf124", "Date: 2025-11-05")
    pdf_path = (
        tmp_path / "meetings" / "ietf124" / "slides"
        / "slides-124-aipref-whiteboard-00.pdf"
    )
    pdf_path.write_bytes(b"\x00 not a valid pdf")
    extract_all_pdfs(str(tmp_path))
    txt = pdf_path.with_suffix(".pdf.txt").read_text()
    # The stub gets the context header so the agent still knows which
    # meeting it would have come from.
    assert "**Meeting:** IETF 124 meeting" in txt
    assert "No extractable text" in txt
