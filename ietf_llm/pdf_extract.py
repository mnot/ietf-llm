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
import re
from dataclasses import dataclass
from typing import List, Optional

from .utils import LogLevel, Verbosity, log

#: Files we never try to extract — they're already text or non-PDF.
_EXCLUDED_SUFFIXES = (".txt", ".md", ".json")


# --- Slide context inference ----------------------------------------------

# Slide file naming: `<meeting>-slides-<session-code>-<topic>-<NN>.pdf`
#   ietf124-slides-124-aipref-overview-00.pdf
#   interim2025aipref08-slides-interim-2025-aipref-08-sessa-draft-status-update-00.pdf
_SLIDE_RE = re.compile(
    r"^(?P<meeting>(?:ietf\d+|interim\d{4}\w+\d+))-slides-"
    r"(?P<middle>.+?)"
    r"-(?P<version>\d+)\.pdf$",
    re.IGNORECASE,
)

# Strip the leading session code from the topic slug. The session code
# typically repeats the meeting identifier ("124-aipref-" or
# "interim-2025-aipref-08-") and optionally an "sessa-" / "sessb-"
# marker that just labels which scheduled session it belonged to.
_SESSION_PREFIX_RE = re.compile(
    r"^(?:\d+-[a-z0-9]+-|interim-\d{4}-[a-z0-9]+-\d+-)(?:sess[a-z]-)?",
    re.IGNORECASE,
)
_BARE_SESSION_PREFIX_RE = re.compile(r"^sess[a-z]-", re.IGNORECASE)

_MEETING_DATE_RE = re.compile(r"^Date:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


@dataclass
class SlideContext:
    meeting: str  # "ietf124" or "interim2025aipref08"
    label: str  # "IETF 124 meeting" / "Interim 2025 #08"
    topic_slug: str  # "vocabulary-status-update"
    version: str  # "01"
    date: Optional[str] = None  # YYYY-MM-DD from companion minutes file
    minutes_file: Optional[str] = None  # `ietf124-minutes.md`


def _meeting_label(meeting_code: str) -> str:
    match = re.match(r"^ietf(\d+)$", meeting_code, re.IGNORECASE)
    if match:
        return f"IETF {match.group(1)} meeting"
    match = re.match(
        r"^interim(\d{4})\w*?(\d+)$", meeting_code, re.IGNORECASE
    )
    if match:
        return f"Interim {match.group(1)} #{match.group(2)}"
    return meeting_code


def _clean_topic(middle: str) -> str:
    stripped = _SESSION_PREFIX_RE.sub("", middle)
    stripped = _BARE_SESSION_PREFIX_RE.sub("", stripped)
    return stripped or middle


def _find_minutes(cache_dir: str, meeting: str) -> Optional[str]:
    for suffix in ("-minutes.md", "-minutes.txt"):
        candidate = os.path.join(cache_dir, f"{meeting}{suffix}")
        if os.path.isfile(candidate):
            return os.path.basename(candidate)
    return None


def _read_meeting_date(cache_dir: str, minutes_name: str) -> Optional[str]:
    try:
        with open(os.path.join(cache_dir, minutes_name), "r", encoding="utf-8") as fh:
            head = fh.read(500)
    except OSError:
        return None
    match = _MEETING_DATE_RE.search(head)
    return match.group(1) if match else None


def slide_context(pdf_name: str, cache_dir: str) -> Optional[SlideContext]:
    """Infer meeting + topic context for a slide PDF, or None if its
    filename doesn't follow the IETF slide convention."""
    match = _SLIDE_RE.match(pdf_name)
    if not match:
        return None
    meeting = match.group("meeting").lower()
    middle = match.group("middle")
    version = match.group("version")
    minutes_name = _find_minutes(cache_dir, meeting)
    date = _read_meeting_date(cache_dir, minutes_name) if minutes_name else None
    return SlideContext(
        meeting=meeting,
        label=_meeting_label(meeting),
        topic_slug=_clean_topic(middle),
        version=version,
        date=date,
        minutes_file=minutes_name,
    )


def _render_slide_header(name: str, context: Optional[SlideContext]) -> str:
    lines = [f"# {name}", ""]
    if context is None:
        return "\n".join(lines) + "\n"
    meeting_text = context.label
    if context.date:
        meeting_text += f" ({context.date})"
    lines.append(f"**Meeting:** {meeting_text}")
    lines.append(f"**Topic slug:** `{context.topic_slug}` (v{context.version})")
    if context.minutes_file:
        lines.append(f"**Minutes:** `{context.minutes_file}`")
    lines.append("")
    return "\n".join(lines) + "\n"


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

        # Slide PDFs get a context header listing the meeting, date
        # (read from the companion minutes file), and topic slug —
        # without it, a chunk from page 5 of a deck has no signal as
        # to which IETF or interim it came from.
        context = slide_context(name, cache_dir)
        header = _render_slide_header(name, context)

        text = extract_pdf_text(pdf_path)
        if not text:
            # Write a tiny stub so we don't retry every gather, and so
            # the .txt's mtime signals "we tried and there's nothing
            # useful here". The stub keeps the context header so an
            # agent reading `_index.md` still knows which meeting the
            # PDF belongs to.
            stub_body = (
                "_No extractable text. The PDF is image-only, encrypted, "
                "or its content stream couldn't be parsed._\n"
            )
            with open(txt_path, "w", encoding="utf-8") as fh:
                fh.write(header + "\n" + stub_body)
            skipped_empty += 1
            written.append(txt_path)
            continue
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(header + "\n" + text)
        written.append(txt_path)

    if written:
        log(
            f"PDF extraction: {len(written)} files "
            f"({skipped_empty} with no extractable text)",
            verbose,
            level=LogLevel.STATUS,
        )
    return written
