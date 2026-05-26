"""Extract text from PDF slide decks (and any other PDFs in the cache).

The cache acquires slide PDFs from `process_meetings` — they're the
deck a chair or presenter showed at an IETF or interim. To an LLM
reading the corpus they're invisible binary blobs by default; this
module writes a sibling `.txt` next to each `.pdf` so the existing
chunker / embedding index pick them up.

Extraction is best-effort. PDFs in the wild include:
  - Slide decks with real text layers (the common case — extracts well)
  - Image-only PDFs (scanned, no text layer — extracts nothing; we
    write a stub `.txt` noting the lack of extractable text so the
    chunker doesn't keep retrying)
  - PDFs with encrypted / DRM-protected content (we skip these and log)

The output `.txt` is plain pages separated by form-feed markers and
a `## Page N` header, so the windowed chunker breaks at sensible
boundaries.
"""

from __future__ import annotations

import os
from typing import List

from .utils import LogLevel, Verbosity, log

#: Files we never try to extract — they're already text or non-PDF.
_EXCLUDED_SUFFIXES = (".txt", ".md", ".json")


def _output_path(pdf_path: str) -> str:
    """Sibling .txt path for a .pdf: `foo.pdf` → `foo.pdf.txt`.

    We keep the original .pdf suffix in the .txt name so it's
    obvious which file the text came from (and so the .txt can't
    collide with a same-named .txt that might already be in the
    cache from another source).
    """
    return pdf_path + ".txt"


def _needs_extraction(pdf_path: str, txt_path: str) -> bool:
    """True if the .txt is missing or older than its source .pdf."""
    if not os.path.exists(txt_path):
        return True
    try:
        return os.path.getmtime(txt_path) < os.path.getmtime(pdf_path)
    except OSError:
        return True


def extract_pdf_text(pdf_path: str) -> str:
    """Extract text from one PDF. Returns "" on unrecoverable errors.

    Each page is preceded by `## Page N` and separated by form feeds
    so the chunker has natural break points.
    """
    # pylint: disable=import-outside-toplevel
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return ""

    try:
        reader = PdfReader(pdf_path)
    except (PdfReadError, OSError, ValueError):
        return ""
    if reader.is_encrypted:
        # We don't carry a passphrase; bail.
        return ""

    parts: List[str] = []
    for idx, page in enumerate(reader.pages, 1):
        try:
            page_text = page.extract_text() or ""
        except Exception:  # pylint: disable=broad-except
            # Page-level extraction can blow up on malformed content
            # streams; skip just that page and continue.
            page_text = ""
        page_text = page_text.strip()
        if not page_text:
            continue
        parts.append(f"## Page {idx}\n\n{page_text}")
    return ("\n\n\f\n\n".join(parts) + "\n") if parts else ""


def extract_all_pdfs(
    cache_dir: str, verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Walk the cache, extract every PDF that needs it. Return paths written.

    Idempotent: re-runs only touch PDFs whose .txt is missing or stale.
    """
    if not os.path.isdir(cache_dir):
        return []
    written: List[str] = []
    skipped_empty = 0
    for name in sorted(os.listdir(cache_dir)):
        if not name.lower().endswith(".pdf"):
            continue
        pdf_path = os.path.join(cache_dir, name)
        if not os.path.isfile(pdf_path):
            continue
        txt_path = _output_path(pdf_path)
        if not _needs_extraction(pdf_path, txt_path):
            continue

        text = extract_pdf_text(pdf_path)
        if not text:
            # Write a tiny stub so we don't retry every gather, and so
            # the .txt's mtime signals "we tried and there's nothing
            # useful here". The stub is also useful to a human reading
            # `_index.md` who wonders why this PDF has no text view.
            stub = (
                f"# {name}\n\n"
                "_No extractable text. The PDF is image-only, encrypted, "
                "or its content stream couldn't be parsed._\n"
            )
            with open(txt_path, "w", encoding="utf-8") as fh:
                fh.write(stub)
            skipped_empty += 1
            written.append(txt_path)
            continue
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\n{text}")
        written.append(txt_path)

    if written:
        log(
            f"PDF extraction: {len(written)} files "
            f"({skipped_empty} with no extractable text)",
            verbose,
            level=LogLevel.STATUS,
        )
    return written
