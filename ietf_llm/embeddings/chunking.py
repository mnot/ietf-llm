"""Content-aware chunking for the semantic-search corpus.

Content-aware chunking, dispatched by filename:

  *-thread-*.md             → one chunk per message section
  *-issue-*.md              → one chunk per comment section
  everything else (.txt/.md) → fixed-size character windows with overlap

The legacy `<wg>-mail-archive-YYYY.txt` and `<wg>-github-<repo>.txt`
year-dumps duplicate the same content in less structured form; they
are excluded from indexing entirely in `_eligible_files`.
"""

from __future__ import annotations

import bisect
import email.utils
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    # Comma-separated lowercased label list for chunks coming from a
    # per-issue file (e.g. "vocabulary,top-level,ready to close"). NULL
    # everywhere else. Every chunk from the same issue file carries the
    # same value — the file-level labels — so a `LIKE %label%` filter at
    # search time can shortlist issues by topic.
    labels: Optional[str] = None
    # Normalised issue state ('open' / 'closed') for chunks from a
    # per-issue file. NULL everywhere else. Lets search prioritise the
    # chairs' resolution over older mid-debate threads, or surface only
    # things the WG hasn't yet decided.
    state: Optional[str] = None
    # Citation URL for the chunk's underlying artefact:
    # - issue chunks: `https://github.com/<owner>/<repo>/issues/<N>`
    #   (the whole file's GitHub URL; every chunk from the same issue
    #   shares it)
    # - thread chunks: the `Archived-At:` permalink for that specific
    #   message (per-chunk; each message has its own URL)
    # - everything else: NULL. Surfaced inline in search hits so the
    #   caller can cite without reconstructing.
    url: Optional[str] = None
    # Issue-cluster signals (per-issue files only). Every chunk from
    # the same issue file carries the same values so a search hit
    # reveals "this is a duplicate" / "closed because…" inline.
    duplicate_of: Optional[int] = None
    closing_rationale: Optional[str] = None


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
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(date_text[: len(fmt) + 2], fmt)
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

# Per-issue file labels line (issue_files._render_issue):
#   "**Labels:** vocabulary, top-level, ready to close  "
_ISSUE_LABELS_RE = re.compile(r"^\*\*Labels:\*\*\s*(.+?)\s*$", re.MULTILINE)

# Per-issue file state line (issue_files._render_issue renders uppercase
# "OPEN" / "CLOSED" but tolerate any case in case the format ever shifts):
#   "**State:** OPEN  "
_ISSUE_STATE_RE = re.compile(r"^\*\*State:\*\*\s*(\S+)", re.MULTILINE)

# Per-issue file URL line (issue_files._render_issue):
#   "**URL:** https://github.com/<owner>/<repo>/issues/<N>  "
_ISSUE_URL_RE = re.compile(r"^\*\*URL:\*\*\s*(\S+)", re.MULTILINE)

# Per-issue file duplicate-of line:
#   "**Duplicate of:** #155"
_ISSUE_DUP_RE = re.compile(r"^\*\*Duplicate of:\*\*\s*#?(\d+)", re.MULTILINE)

# Per-issue file closing-rationale block. The renderer emits
# "**Closing rationale:**\n\n_by Author on Date:_\n\n> quoted body"
# We capture everything up to the next blank line + ## section, since
# the rationale is one block of markdown.
_ISSUE_RATIONALE_RE = re.compile(
    r"^\*\*Closing rationale:\*\*\s*\n+(.+?)(?=\n\n##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Per-thread message Archived-At line (mail_threads._render_thread).
# One per message section, italicised inline form:
#   "_Archived-At:_ https://mailarchive.ietf.org/arch/msg/<list>/<tok>/"
_THREAD_ARCHIVED_AT_RE = re.compile(r"^_Archived-At:_\s*(\S+)", re.MULTILINE)


def _extract_issue_labels(text: str) -> Optional[str]:
    """Pull the `**Labels:**` line out of a per-issue file's header
    and return a normalised comma-separated lowercase string.

    Returns None if the file has no labels line (issues created without
    any GitHub labels won't have one). Whitespace around individual
    labels is trimmed; the original ordering is preserved.
    """
    match = _ISSUE_LABELS_RE.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    parts = [p.strip().lower() for p in raw.split(",")]
    parts = [p for p in parts if p]
    return ",".join(parts) if parts else None


def _extract_issue_url(text: str) -> Optional[str]:
    """Pull the `**URL:**` line out of a per-issue file's header.

    Returns None for malformed archives where the line is missing
    (e.g. a `repo` field without a `/` so issue_files skipped the URL).
    """
    match = _ISSUE_URL_RE.search(text)
    return match.group(1).strip() if match else None


def _extract_issue_duplicate_of(text: str) -> Optional[int]:
    """Pull the `**Duplicate of:**` line and return its #N as an int."""
    match = _ISSUE_DUP_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _extract_issue_rationale(text: str) -> Optional[str]:
    """Pull the `**Closing rationale:**` block out of a per-issue file.

    Returns the block content (the formatted markdown beneath the
    label), suitable for direct inclusion in search-hit output. None
    if the issue isn't closed or carries no comments.
    """
    match = _ISSUE_RATIONALE_RE.search(text)
    return match.group(1).strip() if match else None


def _extract_thread_archived_at(text: str) -> Optional[str]:
    """First `_Archived-At:_` URL in a thread message section.

    `text` here is the body of one message section (between two
    `### [N] ...` headers), so taking the first match is safe — there
    can only be one Archived-At per message.
    """
    match = _THREAD_ARCHIVED_AT_RE.search(text)
    return match.group(1).strip() if match else None


def _extract_issue_state(text: str) -> Optional[str]:
    """Pull the `**State:**` line and normalise to 'open' / 'closed'.

    Anything else (an unexpected state string) is returned lowercased
    as-is rather than dropped — better to surface a surprise than to
    silently swallow it. Returns None if the file has no state line.
    """
    match = _ISSUE_STATE_RE.search(text)
    if not match:
        return None
    return match.group(1).strip().lower() or None


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
                chunk_date=(_normalize_to_utc_iso(date_m.group(1)) if date_m else None),
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
                chunk_date=(_normalize_to_utc_iso(date_m.group(1)) if date_m else None),
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
    """Split a `<wg>-thread-*.md` or `<wg>-issue-*.md` file into one
    chunk per message / comment section.

    Both file types share a format (mail_threads._render_thread and
    issue_files._render_issue): metadata header, an outline, then one
    `### [N] DATE — Sender` section per message/comment. We chunk on
    those section boundaries so the embedding index has one row per
    actual message — fine-grained retrieval matching the per-thread /
    per-issue reading view.

    For issue files only, we additionally extract the `**Labels:**`
    line from the header and stamp the comma-separated label list onto
    every chunk in the file, so search can filter by topic label.
    """
    line_starts = _build_line_index(text)
    matches = list(_THREAD_MSG_RE.finditer(text))
    chunks: List[Chunk] = []

    if not matches:
        # Malformed thread file (shouldn't happen for ones we generate);
        # fall back to windowed chunking so something is still indexed.
        return _chunk_windowed(text, filename)

    # Issue files have `**Labels:**` and `**State:**` header lines;
    # thread files don't. Stamping the file-level values onto every
    # chunk lets search-time filters (label="top-level", state="closed")
    # shortlist by curation + resolution before semantic ranking.
    # Post-reorg, issue files live under `issues/<repo>/<N>.md`.
    is_issue = filename.lower().startswith("issues/")
    labels = _extract_issue_labels(text) if is_issue else None
    state = _extract_issue_state(text) if is_issue else None
    # Citation URL: for issue files the URL is file-level (every chunk
    # from the same issue shares it); for thread files each message
    # section carries its own `_Archived-At:_` line, so we pull the
    # URL per-chunk below.
    file_url = _extract_issue_url(text) if is_issue else None
    # Issue-cluster signals: duplicate-of marker and closing rationale.
    # File-level: every chunk from this issue inherits them, so a
    # search hit reveals "this is a dup" or "closed because…" inline.
    duplicate_of = _extract_issue_duplicate_of(text) if is_issue else None
    closing_rationale = _extract_issue_rationale(text) if is_issue else None

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
                labels=labels,
                state=state,
                url=file_url,
                duplicate_of=duplicate_of,
                closing_rationale=closing_rationale,
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
        # Per-chunk URL: issue chunks inherit the file-level URL;
        # thread chunks have their own per-message Archived-At line.
        chunk_url = file_url if is_issue else _extract_thread_archived_at(body)
        chunks.append(
            Chunk(
                file=filename,
                chunk_idx=len(chunks),
                title=title,
                text=body[:MAX_CHUNK_CHARS],
                start_line=_line_at(line_starts, start_off),
                end_line=_line_at(line_starts, max(end_off - 1, 0)),
                chunk_date=chunk_date,
                labels=labels,
                state=state,
                url=chunk_url,
                duplicate_of=duplicate_of,
                closing_rationale=closing_rationale,
            )
        )
    return chunks


def _chunk_file(path: str, relpath: str) -> List[Chunk]:
    """Read a cache file and dispatch to the right chunker based on its
    location in the cache layout.

    `relpath` is the path relative to the WG cache dir; that's what
    chunks store as `file` and what dispatch keys off (post-reorg).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    lower = relpath.lower()
    # Per-thread reconstructions are the LLM-legible mailing-list form.
    if lower.startswith("threads/") and lower.endswith(".md"):
        return _chunk_thread_file(text, relpath)
    # Per-issue reconstructions share the thread file format.
    if lower.startswith("issues/") and lower.endswith(".md"):
        return _chunk_thread_file(text, relpath)
    return _chunk_windowed(text, relpath)


def _eligible_files(cache_dir: str, wg: str) -> List[str]:  # noqa: ARG001
    """Return absolute paths of files worth embedding.

    Walks the WG cache recursively. Skips:
      - `digests/` (those are the catalogue surface, not indexed)
      - `github/` (raw archive JSON)
      - `raw/` (legacy text dumps kept for grep / NotebookLM)
      - `meetings/<code>/slides/*.pdf` (binaries — we index the
        sibling `.pdf.txt` extracts instead)
    """
    out = []
    for dirpath, _dirnames, filenames in os.walk(cache_dir):
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            relpath = os.path.relpath(path, cache_dir)
            relpath_lower = relpath.lower()
            if relpath_lower.startswith("digests/"):
                continue
            if relpath_lower.startswith("github/"):
                continue
            if relpath_lower.startswith("raw/"):
                continue
            if name.endswith(".pdf") or name.endswith(".json"):
                continue
            if not (name.endswith(".txt") or name.endswith(".md")):
                continue
            out.append(path)
    return out
