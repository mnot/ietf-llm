"""Content-aware chunking for the semantic-search corpus.

Three chunking strategies, dispatched by filename:

  *-mailing-list-YYYY.txt   → one chunk per message  (===... separator)
  *-github-<repo>.txt       → one chunk per issue    (===... separator)
  everything else (.txt/.md) → fixed-size character windows with overlap

Per-message and per-issue chunks give clean citations; the windowed
fallback handles drafts, RFCs, transcripts, and minutes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List

CHUNK_SIZE = 2000  # characters
CHUNK_OVERLAP = 200
MAX_CHUNK_CHARS = 8000  # hard cap per chunk sent to the embedding model


@dataclass
class Chunk:
    file: str  # basename
    chunk_idx: int  # ordinal within the file
    title: str  # subject / issue title / section hint, for display
    text: str


_RECORD_SEP = re.compile(r"\n=+\n+", re.MULTILINE)
_SUBJECT_RE = re.compile(r"^Subject:\s*(.+)$", re.MULTILINE)
_FROM_RE = re.compile(r"^From:\s*(.+)$", re.MULTILINE)
_DATE_RE = re.compile(r"^Date:\s*(.+)$", re.MULTILINE)
_ISSUE_RE = re.compile(r"^Issue #(\d+):\s*(.+)$", re.MULTILINE)


def _chunk_message_file(text: str, filename: str) -> List[Chunk]:
    """Split a mailing-list-YYYY.txt file into one chunk per message."""
    parts = [p.strip() for p in _RECORD_SEP.split(text) if p.strip()]
    chunks: List[Chunk] = []
    for idx, part in enumerate(parts):
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
            Chunk(file=filename, chunk_idx=idx, title=title, text=part[:MAX_CHUNK_CHARS])
        )
    return chunks


def _chunk_issues_file(text: str, filename: str) -> List[Chunk]:
    """Split a github-<repo>.txt file into one chunk per issue."""
    parts = [p.strip() for p in _RECORD_SEP.split(text) if p.strip()]
    chunks: List[Chunk] = []
    for idx, part in enumerate(parts):
        iss_m = _ISSUE_RE.search(part)
        if iss_m:
            title = f"#{iss_m.group(1)}: {iss_m.group(2).strip()}"
        else:
            title = f"record {idx}"
        chunks.append(
            Chunk(file=filename, chunk_idx=idx, title=title, text=part[:MAX_CHUNK_CHARS])
        )
    return chunks


def _chunk_windowed(text: str, filename: str) -> List[Chunk]:
    """Fixed-size character chunks with overlap, for drafts/RFCs/etc."""
    chunks: List[Chunk] = []
    text = text.strip()
    if not text:
        return chunks
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
            Chunk(file=filename, chunk_idx=idx, title=title, text=body)
        )
        idx += 1
        pos += step
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
    if "mailing-list" in lower:
        return _chunk_message_file(text, filename)
    if "-github-" in lower and lower.endswith(".txt"):
        return _chunk_issues_file(text, filename)
    return _chunk_windowed(text, filename)


def _eligible_files(cache_dir: str, wg: str) -> List[str]:
    """Files worth embedding; skip digests, JSON, binaries."""
    out = []
    for name in sorted(os.listdir(cache_dir)):
        if name.startswith(f"{wg}-_"):
            continue
        if name.endswith(".json") or name.endswith(".pdf"):
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        if not (name.endswith(".txt") or name.endswith(".md")):
            continue
        out.append(path)
    return out
