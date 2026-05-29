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

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

from ..paths import meeting_code_for_relpath, meeting_label, minutes_path
from ..utils import LogLevel, Verbosity, log

# pypdf emits a steady stream of "Ignoring wrong pointing object N 0
# (offset 0)" warnings on slide decks exported from PowerPoint / Keynote
# — those decks have a quirky xref table that pypdf works around fine
# but still complains about. The warnings are pure noise (extraction
# succeeds anyway); silence them at module load so a gather doesn't
# scroll dozens of unactionable lines past the user.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: Files we never try to extract — they're already text or non-PDF.
_EXCLUDED_SUFFIXES = (".txt", ".md", ".json")


# --- Slide context inference ----------------------------------------------

# Slide file naming (post-reorg): the meeting code lives in the path
# as `meetings/<code>/slides/<filename>.pdf`, so we no longer need to
# parse it out of the filename. The filename itself still carries
# `slides-<session-code>-<topic>-<NN>.pdf` from Datatracker.
_SLIDE_BASENAME_RE = re.compile(
    r"^slides-(?P<middle>.+?)-(?P<version>\d+)\.pdf$",
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
    # Shared parser in paths.py — handles ietf<N>, date-coded
    # clustered interims, and legacy per-session interim codes.
    return meeting_label(meeting_code)


def _clean_topic(middle: str) -> str:
    stripped = _SESSION_PREFIX_RE.sub("", middle)
    stripped = _BARE_SESSION_PREFIX_RE.sub("", stripped)
    return stripped or middle


def _find_minutes_relpath(cache_dir: str, meeting: str) -> Optional[str]:
    """Return the relative path to the meeting's minutes file (if any).
    Returns the path relative to cache_dir so it round-trips through
    consumers that expect that form."""
    candidate = minutes_path(cache_dir, meeting)
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, cache_dir)
    return None


def _read_meeting_date(cache_dir: str, minutes_relpath: str) -> Optional[str]:
    try:
        with open(
            os.path.join(cache_dir, minutes_relpath), "r", encoding="utf-8",
        ) as fh:
            head = fh.read(500)
    except OSError:
        return None
    match = _MEETING_DATE_RE.search(head)
    return match.group(1) if match else None


def slide_context(
    pdf_relpath: str, cache_dir: str,
) -> Optional[SlideContext]:
    """Infer meeting + topic context for a slide PDF.

    `pdf_relpath` is the path relative to cache_dir, e.g.
    `meetings/ietf124/slides/slides-124-aipref-overview-00.pdf`.
    The meeting code comes from the path; topic + version come from
    the filename. Returns None if the basename doesn't follow the
    `slides-…-NN.pdf` convention.
    """
    basename = os.path.basename(pdf_relpath)
    match = _SLIDE_BASENAME_RE.match(basename)
    if not match:
        return None
    meeting = meeting_code_for_relpath(pdf_relpath)
    if not meeting:
        return None
    middle = match.group("middle")
    version = match.group("version")
    minutes_rel = _find_minutes_relpath(cache_dir, meeting)
    date = _read_meeting_date(cache_dir, minutes_rel) if minutes_rel else None
    return SlideContext(
        meeting=meeting,
        label=_meeting_label(meeting),
        topic_slug=_clean_topic(middle),
        version=version,
        date=date,
        minutes_file=minutes_rel,
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
    """Walk the cache (recursively), extract every PDF that needs it.

    Idempotent: re-runs only touch PDFs whose .txt is missing or stale.
    Walks recursively because slide PDFs now live under
    `meetings/<code>/slides/`, not flat at the top of the cache.
    """
    if not os.path.isdir(cache_dir):
        return []
    written: List[str] = []
    skipped_empty = 0
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in sorted(filenames):
            if not name.lower().endswith(".pdf"):
                continue
            pdf_path = os.path.join(dirpath, name)
            relpath = os.path.relpath(pdf_path, cache_dir)
            if not os.path.isfile(pdf_path):
                continue
            txt_path = _output_path(pdf_path)
            if not _needs_extraction(pdf_path, txt_path):
                continue

            # Slide PDFs get a context header listing the meeting,
            # date (read from the companion minutes file), and topic
            # slug — without it, a chunk from page 5 of a deck has no
            # signal as to which IETF or interim it came from.
            context = slide_context(relpath, cache_dir)
            header = _render_slide_header(name, context)

            text = extract_pdf_text(pdf_path)
            if not text:
                # Write a tiny stub so we don't retry every gather, and so
                # the .txt's mtime signals "we tried and there's nothing
                # useful here". The stub keeps the context header so an
                # agent reading the digests still knows which meeting
                # the PDF belongs to.
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
