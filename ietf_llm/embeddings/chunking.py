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
from typing import Any, Dict, List, Optional

from ..gather.documents_manifest import skip_embed_draft_names
from ..gather.drafts import normalize_draft_name

# Character budget per embedded chunk. The embedding model only embeds the
# first ~512 tokens of whatever text it's given; anything past that is silently
# dropped from the vector (though still stored for display). We therefore size
# chunks in characters to stay under that window. Calibrated offline against
# real corpora (httpbis/tls/aipref/rswg/last-call) by tokenising every message
# section + windowed slice with the bge-small-en-v1.5 tokenizer: the section
# tok/char ratio runs p50≈0.34, p99≈0.46, max≈0.52, so ~1200 chars keeps the
# bulk (≈p95) of fragments under ~510 content tokens. Denser outliers (code,
# URLs, base64) can still exceed it — the model truncates as a backstop, but
# the `EMBED_CHAR_OVERLAP` between fragments means a tail dropped from one
# fragment is re-covered by the head of the next, so content stays searchable.
# Char-based on purpose: chunking must NOT depend on a runtime tokenizer (the
# remote embedding backend won't ship one). Bumping this is a write-side change
# — bump CHUNKER_VERSION in search.py so existing indices re-embed.
EMBED_CHAR_BUDGET = 1200
EMBED_CHAR_OVERLAP = 150
MAX_CHUNK_CHARS = 8000  # hard cap for the legacy year-dump chunkers (dead path)

# Identifies the chunking contract that produced an index. Stored in the
# index `meta` table and compared at build time like the model id: a
# mismatch forces a full re-embed of that WG on its next gather, so a
# change to chunk boundaries (which the model-id check can't detect)
# transparently rebuilds. Bump on any change to how chunks are cut.
#   "1" — fixed 2000-char windows, 8000-char per-section truncation
#   "2" — EMBED_CHAR_BUDGET windows; long sections split into sub_idx
#         fragments instead of being truncated
CHUNKER_VERSION = "2"


@dataclass
class Chunk:
    file: str  # basename
    chunk_idx: int  # ordinal within the file
    title: str  # subject / issue title / section hint, for display
    text: str
    # Sub-fragment ordinal within a (file, chunk_idx). A message/comment
    # section longer than EMBED_CHAR_BUDGET is windowed into several
    # fragments that all share the same chunk_idx (so the message-number ==
    # chunk_idx invariant the reader tools rely on is preserved) but get
    # distinct sub_idx values and distinct embedding vectors. sub_idx 0
    # carries the FULL section text (so get_chunk / get_messages can return
    # the whole message) while embedding only its first window; sub_idx ≥1
    # carry their window slice as both text and embedded text. Short,
    # in-budget sections (the common case) are a single sub_idx 0 chunk.
    sub_idx: int = 0
    # Text actually fed to the embedding model. None means "embed `text`".
    # Set on sub_idx 0 of a split section, where `text` holds the full
    # message but only the first window should be embedded. Never stored —
    # it exists only to drive build_index's embed call.
    embed_text: Optional[str] = None
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


def _window_text(text: str, budget: int, overlap: int) -> List[tuple[str, int, int]]:
    """Slide a fixed-size window over `text`, returning (sub, start, end)
    for each window where start/end are byte offsets *within `text`*.

    Consecutive windows overlap by exactly `overlap` chars, so a token
    dropped from the tail of one window (when a dense window tokenises past
    the model's 512-token limit) is re-covered by the head of the next.
    The single shared windowing primitive for both the windowed chunker and
    the per-section splitter, so line-number mapping stays identical for
    every sub-chunk. A sub-budget `text` returns one window spanning it all.
    """
    if not text:
        return []
    step = max(1, budget - overlap)
    out: List[tuple[str, int, int]] = []
    pos = 0
    length = len(text)
    while pos < length:
        end = min(pos + budget, length)
        out.append((text[pos:end], pos, end))
        if end >= length:
            break
        pos += step
    return out


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

# Charter source line (gather.charter writes it as the second line):
#   "Source: https://www.ietf.org/charter/charter-ietf-<wg>-<rev>.txt"
_CHARTER_SOURCE_RE = re.compile(r"^Source:\s*(\S+)", re.MULTILINE)


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


def _windowed_citation_url(relpath: str, text: str) -> Optional[str]:
    """File-level citation URL for a windowed (non-thread/issue) file.

    Stamped on every chunk of the file so an agent can cite the source
    without reconstructing the URL itself (the exact step that invites a
    wrong or hallucinated citation). Covers only the cases with an
    unambiguous canonical URL:

    - `drafts/draft-<name>-NN.txt` → the Datatracker doc page,
      version-agnostic (`/doc/<name>/` resolves to the latest revision,
      which is what a citation should point at).
    - `charter.txt` → the `Source:` URL gather already wrote into the
      file header.

    Everything else returns None — RFC bodies (cite via `get_rfc`),
    minutes, transcripts, pdf extracts. NULL is deliberate there: a URL
    we cannot construct with confidence is worse stamped than absent.
    """
    lower = relpath.lower()
    if lower.startswith("drafts/") and lower.endswith(".txt"):
        name = normalize_draft_name(os.path.basename(relpath))
        # RFC text files normalise to `rfcNNNN` (no `draft-` prefix);
        # leave those to get_rfc rather than minting a /doc/ URL here.
        if name.startswith("draft-"):
            return f"https://datatracker.ietf.org/doc/{name}/"
        return None
    if lower == "charter.txt" or lower.endswith("/charter.txt"):
        match = _CHARTER_SOURCE_RE.search(text)
        if match:
            return match.group(1).strip()
    return None


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
    """Budget-sized character chunks with overlap, for drafts/RFCs/etc.

    Each window is its own chunk_idx (sub_idx 0); windows are sized to
    EMBED_CHAR_BUDGET so the whole window fits the model's token budget
    rather than having its tail silently dropped from the vector.
    """
    chunks: List[Chunk] = []
    text = text.strip()
    if not text:
        return chunks
    # File-level citation URL (drafts, charter) stamped on every window;
    # None for windowed files without a confident canonical URL.
    file_url = _windowed_citation_url(filename, text)
    line_starts = _build_line_index(text)
    for idx, (body, start, end) in enumerate(
        _window_text(text, EMBED_CHAR_BUDGET, EMBED_CHAR_OVERLAP)
    ):
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
                start_line=_line_at(line_starts, start),
                end_line=_line_at(line_starts, max(end - 1, start)),
                url=file_url,
            )
        )
    return chunks


def _section_chunks(
    *,
    file: str,
    chunk_idx: int,
    title: str,
    body: str,
    sec_off: int,
    line_starts: List[int],
    meta: Dict[str, Any],
) -> List[Chunk]:
    """Emit the chunk row(s) for one section (thread header or message).

    In-budget section → a single sub_idx 0 chunk embedding its full text.
    Over-budget section → windowed: sub_idx 0 carries the full body (with
    the section's full line span) but embeds only its first window; later
    sub_idx carry their window slice and its own line span. Every fragment
    shares `chunk_idx`, `meta`, and the title (with a `(part k/n)` hint so a
    search hit on a tail fragment is legible; storage strips the hint when
    it reconstitutes the whole message).
    """
    windows = _window_text(body, EMBED_CHAR_BUDGET, EMBED_CHAR_OVERLAP)
    full_start = _line_at(line_starts, sec_off)
    full_end = _line_at(line_starts, sec_off + max(len(body) - 1, 0))
    if len(windows) <= 1:
        return [
            Chunk(
                file=file,
                chunk_idx=chunk_idx,
                sub_idx=0,
                title=title,
                text=body,
                start_line=full_start,
                end_line=full_end,
                **meta,
            )
        ]
    parts = len(windows)
    out: List[Chunk] = []
    for k, (sub, start, end) in enumerate(windows):
        part_title = f"{title} (part {k + 1}/{parts})"
        if k == 0:
            # sub_idx 0: full body for retrieval, first window for the vector.
            out.append(
                Chunk(
                    file=file,
                    chunk_idx=chunk_idx,
                    sub_idx=0,
                    title=part_title,
                    text=body,
                    embed_text=sub,
                    start_line=full_start,
                    end_line=full_end,
                    **meta,
                )
            )
        else:
            out.append(
                Chunk(
                    file=file,
                    chunk_idx=chunk_idx,
                    sub_idx=k,
                    title=part_title,
                    text=sub,
                    start_line=_line_at(line_starts, sec_off + start),
                    end_line=_line_at(line_starts, sec_off + max(end - 1, start)),
                    **meta,
                )
            )
    return out


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
    # It's chunk_idx 0 by convention; the reply graph and position tally
    # both treat message number `[N]` as chunk_idx N, so the header — which
    # has no message number — takes 0.
    file_meta = {
        "labels": labels,
        "state": state,
        "duplicate_of": duplicate_of,
        "closing_rationale": closing_rationale,
    }
    header_end = matches[0].start()
    header_text = text[:header_end].strip()
    if header_text:
        chunks.extend(
            _section_chunks(
                file=filename,
                chunk_idx=0,
                title="(thread header)",
                body=header_text,
                sec_off=0,
                line_starts=line_starts,
                meta={**file_meta, "url": file_url},
            )
        )

    for i, match in enumerate(matches):
        start_off = match.start()
        end_off = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start_off:end_off].strip()
        if not body:
            continue
        # chunk_idx is the message number `[N]` (regex group 1), NOT a
        # running counter — a long message that splits into several
        # sub_idx fragments must not push later messages' chunk_idx out of
        # step with the `[N]` the reply graph / position tally key on.
        msg_idx = int(match.group(1))
        # Title: "[N] DATE — Sender" without the leading `### `.
        title = match.group(0)[4:].strip()
        # Date is group 2 in the regex (e.g. "2026-04-13 10:00").
        chunk_date = _normalize_to_utc_iso(match.group(2))
        # Per-chunk URL: issue chunks inherit the file-level URL;
        # thread chunks have their own per-message Archived-At line.
        chunk_url = file_url if is_issue else _extract_thread_archived_at(body)
        chunks.extend(
            _section_chunks(
                file=filename,
                chunk_idx=msg_idx,
                title=title,
                body=body,
                sec_off=start_off,
                line_starts=line_starts,
                meta={**file_meta, "chunk_date": chunk_date, "url": chunk_url},
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


def _eligible_files(cache_dir: str, wg: str) -> List[str]:
    """Return absolute paths of files worth embedding.

    Walks the WG cache recursively. Skips:
      - `digests/` (those are the catalogue surface, not indexed)
      - `github/` (raw archive JSON)
      - `raw/` (legacy text dumps kept for grep / NotebookLM)
      - `meetings/<code>/slides/*.pdf` (binaries — we index the
        sibling `.pdf.txt` extracts instead)
      - `drafts/draft-…-NN.txt` revisions of a draft whose Datatracker
        state is `rfc` / `repl` (see `skip_embed_draft_names`): the
        content is canonical in the published RFC or the replacing
        draft, so the revision stack is historical noise. The files stay
        on disk (read / cite / grep); only embedding is gated. RFC
        `.txt` files and active / expired drafts are unaffected.
    """
    skip_drafts = skip_embed_draft_names(wg)
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
            if (
                skip_drafts
                and relpath_lower.startswith("drafts/")
                and name.startswith("draft-")
                and normalize_draft_name(name) in skip_drafts
            ):
                continue
            out.append(path)
    return out
