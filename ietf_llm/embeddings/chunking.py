"""Content-aware chunking for the semantic-search corpus.

Three chunking strategies, dispatched by filename:

  *-mailing-list-YYYY.txt   → one chunk per message  (===... separator)
  *-github-<repo>.txt       → one chunk per issue    (===... separator)
  everything else (.txt/.md) → fixed-size character windows with overlap

Per-message and per-issue chunks give clean citations; the windowed
fallback handles drafts, RFCs, transcripts, and minutes.
"""

from __future__ import annotations

import bisect
import email.utils
import os
import re
from dataclasses import dataclass
from datetime import timezone
from typing import List, Optional

CHUNK_SIZE = 2000  # characters
CHUNK_OVERLAP = 200
MAX_CHUNK_CHARS = 8000  # hard cap per chunk sent to the embedding model


@dataclass
class Chunk:
    file: str  # basename
    chunk_idx: int  # ordinal within the file
    title: str  # subject / issue title / section hint, for display
    text: str
    # 1-indexed inclusive line range within the source file. Allow None
    # so legacy code paths and tests can construct chunks without them
    # (the index migration also leaves them NULL on pre-v2 rows).
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    # ISO 8601 UTC ("YYYY-MM-DDTHH:MM:SSZ") for chunks where time is a
    # meaningful axis (mailing-list messages, GitHub issues). NULL for
    # windowed chunks of drafts/RFCs/transcripts where it isn't.
    chunk_date: Optional[str] = None


def _normalize_to_utc_iso(date_text: str) -> Optional[str]:
    """Parse an email/issue/thread date string and return ISO 8601 UTC.

    Used for the `chunk_date` column. Picks UTC so SQL string
    comparison gives correct chronological order regardless of the
    source timezone. Tolerates the three real formats we see:
      - RFC 5322 from .eml headers ("Mon, 01 Jan 2025 10:00:00 +0000")
      - github.py's `format_date` output ("2025-01-01 10:00:00 UTC")
      - Per-thread file section headers ("2025-01-01 10:00", no seconds)
    """
    date_text = date_text.strip()
    if not date_text:
        return None
    # Try RFC 5322 first (mail headers).
    try:
        parsed = email.utils.parsedate_to_datetime(date_text)
    except (ValueError, TypeError, IndexError):
        parsed = None
    # Fall back to the two "YYYY-MM-DD HH:MM[:SS] [TZ]" forms.
    if parsed is None:
        # pylint: disable=import-outside-toplevel
        from datetime import datetime as _dt

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = _dt.strptime(date_text[: len(fmt) + 2], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_line_index(text: str) -> List[int]:
    """Return byte offsets where each line starts.

    `line_starts[n]` is the byte offset of line `n+1` (0-indexed list,
    1-indexed line numbers). Used to convert chunk byte-offsets back
    to source-file line numbers in O(log n) via bisect.
    """
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_at(line_starts: List[int], offset: int) -> int:
    """1-indexed line number containing the given byte offset."""
    return bisect.bisect_right(line_starts, offset)


_RECORD_SEP = re.compile(r"\n=+\n+", re.MULTILINE)
_SUBJECT_RE = re.compile(r"^Subject:\s*(.+)$", re.MULTILINE)
_FROM_RE = re.compile(r"^From:\s*(.+)$", re.MULTILINE)
_DATE_RE = re.compile(r"^Date:\s*(.+)$", re.MULTILINE)
_ISSUE_RE = re.compile(r"^Issue #(\d+):\s*(.+)$", re.MULTILINE)

# Per-thread file message section header:
#   "### [N] YYYY-MM-DD HH:MM — Sender (reply to [M])"
_THREAD_MSG_RE = re.compile(
    r"^### \[(\d+)\] (\S+(?:\s+\S+)?) — (.+?)(?: \(reply to \[\d+\]\))?$",
    re.MULTILINE,
)


def _record_spans(text: str) -> List[tuple[int, int]]:
    """Return (start, end) byte offsets for each record between separators.

    Mirrors `_RECORD_SEP.split(text)` but preserves positions in the
    original text so chunks can carry line numbers back to the source.
    """
    spans: List[tuple[int, int]] = []
    cursor = 0
    for match in _RECORD_SEP.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def _chunk_message_file(text: str, filename: str) -> List[Chunk]:
    """Split a mailing-list-YYYY.txt file into one chunk per message."""
    line_starts = _build_line_index(text)
    chunks: List[Chunk] = []
    for idx, (start_off, end_off) in enumerate(_record_spans(text)):
        part = text[start_off:end_off].strip()
        if not part:
            continue
        subj_m = _SUBJECT_RE.search(part)
        from_m = _FROM_RE.search(part)
        date_m = _DATE_RE.search(part)
        title_bits = []
        if subj_m:
            title_bits.append(subj_m.group(1).strip())
        if from_m:
            title_bits.append(f"from {from_m.group(1).strip()}")
        if date_m:
            title_bits.append(date_m.group(1).strip()[:25])
        title = " · ".join(title_bits) or f"message {idx}"
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=idx,
                title=title,
                text=part[:MAX_CHUNK_CHARS],
                start_line=_line_at(line_starts, start_off),
                # max(end_off - 1, 0): byte at end_off is past the part
                end_line=_line_at(line_starts, max(end_off - 1, 0)),
                chunk_date=(
                    _normalize_to_utc_iso(date_m.group(1)) if date_m else None
                ),
            )
        )
    return chunks


def _chunk_issues_file(text: str, filename: str) -> List[Chunk]:
    """Split a github-<repo>.txt file into one chunk per issue."""
    line_starts = _build_line_index(text)
    chunks: List[Chunk] = []
    for idx, (start_off, end_off) in enumerate(_record_spans(text)):
        part = text[start_off:end_off].strip()
        if not part:
            continue
        iss_m = _ISSUE_RE.search(part)
        if iss_m:
            title = f"#{iss_m.group(1)}: {iss_m.group(2).strip()}"
        else:
            title = f"record {idx}"
        date_m = _DATE_RE.search(part)
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=idx,
                title=title,
                text=part[:MAX_CHUNK_CHARS],
                start_line=_line_at(line_starts, start_off),
                end_line=_line_at(line_starts, max(end_off - 1, 0)),
                chunk_date=(
                    _normalize_to_utc_iso(date_m.group(1)) if date_m else None
                ),
            )
        )
    return chunks


def _chunk_windowed(text: str, filename: str) -> List[Chunk]:
    """Fixed-size character chunks with overlap, for drafts/RFCs/etc."""
    chunks: List[Chunk] = []
    text = text.strip()
    if not text:
        return chunks
    line_starts = _build_line_index(text)
    step = CHUNK_SIZE - CHUNK_OVERLAP
    idx = 0
    pos = 0
    while pos < len(text):
        body = text[pos : pos + CHUNK_SIZE]
        # First non-empty line as title hint
        title = next((ln.strip() for ln in body.splitlines() if ln.strip()), filename)
        if len(title) > 80:
            title = title[:77] + "..."
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=idx,
                title=title,
                text=body,
                start_line=_line_at(line_starts, pos),
                end_line=_line_at(line_starts, pos + len(body) - 1),
            )
        )
        idx += 1
        pos += step
    return chunks


def _chunk_thread_file(text: str, filename: str) -> List[Chunk]:
    """Split a `<wg>-thread-*.md` file into one chunk per message section.

    The thread file format is fixed (see mail_threads._render_thread):
    a metadata header, an outline, then one `### [N] DATE — Sender`
    section per message. We chunk on those section boundaries so the
    embedding index has one row per actual message — fine-grained
    retrieval matching the per-thread reading view.
    """
    line_starts = _build_line_index(text)
    matches = list(_THREAD_MSG_RE.finditer(text))
    chunks: List[Chunk] = []

    if not matches:
        # Malformed thread file (shouldn't happen for ones we generate);
        # fall back to windowed chunking so something is still indexed.
        return _chunk_windowed(text, filename)

    # Header (subject + outline) is everything before the first message.
    header_end = matches[0].start()
    header_text = text[:header_end].strip()
    if header_text:
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=0,
                title="(thread header)",
                text=header_text[:MAX_CHUNK_CHARS],
                start_line=1,
                end_line=_line_at(line_starts, max(header_end - 1, 0)),
            )
        )

    for i, match in enumerate(matches):
        start_off = match.start()
        end_off = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start_off:end_off].strip()
        if not body:
            continue
        # Title: "[N] DATE — Sender" without the leading `### `.
        title = match.group(0)[4:].strip()
        # Date is group 2 in the regex (e.g. "2026-04-13 10:00").
        chunk_date = _normalize_to_utc_iso(match.group(2))
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=len(chunks),
                title=title,
                text=body[:MAX_CHUNK_CHARS],
                start_line=_line_at(line_starts, start_off),
                end_line=_line_at(line_starts, max(end_off - 1, 0)),
                chunk_date=chunk_date,
            )
        )
    return chunks


def _chunk_file(path: str) -> List[Chunk]:
    """Read a cache file and dispatch to the right chunker based on its name."""
    filename = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    lower = filename.lower()
    # Per-thread reconstructions are the LLM-legible mailing-list form.
    # Match before "mailing-list" so order doesn't matter.
    if "-thread-" in lower and lower.endswith(".md"):
        return _chunk_thread_file(text, filename)
    # Per-issue reconstructions share the thread file format (### [N] DATE — Author),
    # so the same chunker applies.
    if "-issue-" in lower and lower.endswith(".md"):
        return _chunk_thread_file(text, filename)
    if "mailing-list" in lower:
        return _chunk_message_file(text, filename)
    if "-github-" in lower and lower.endswith(".txt"):
        return _chunk_issues_file(text, filename)
    return _chunk_windowed(text, filename)


def _eligible_files(cache_dir: str, wg: str) -> List[str]:
    """Files worth embedding; skip digests, JSON, binaries, and the
    legacy mailing-list-YYYY year-dumps.

    The year-files are kept on disk for grep / NotebookLM upload but
    excluded from the embedding index because the per-thread
    reconstructions (`<wg>-thread-*.md`) cover exactly the same
    message content in a structured form. Indexing both would
    double-count every message and pollute search rankings.
    """
    out = []
    for name in sorted(os.listdir(cache_dir)):
        if name.startswith(f"{wg}-_"):
            continue
        if name.endswith(".json") or name.endswith(".pdf"):
            continue
        # Legacy year-dumps duplicate content now in per-thread files.
        if "mailing-list" in name.lower() and name.endswith(".txt"):
            continue
        # Per-issue .md files duplicate the content of the github-<repo>.txt
        # blob; skip the big blob in favour of the structured per-issue files.
        if "-github-" in name.lower() and name.endswith(".txt"):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        if not (name.endswith(".txt") or name.endswith(".md")):
            continue
        out.append(path)
    return out
