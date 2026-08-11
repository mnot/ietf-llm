"""Recover readable section text from the index's byte ranges.

The published index says where every chunk lives — `(rfc, off, len)` — but
carries no prose. The obvious way to get the prose back is to re-run
rfc.fyi's chunker over a mirror and join on those keys. That turns out to be
unnecessary: a section's chunks are contiguous in the source (gaps between
them are median 5 bytes, just paragraph whitespace), so **one slice of the
union span is the section**, and no join is needed at all.

That is worth stating plainly because it removes a lot: no fetching or
vendoring of another project's chunker, no executing code pinned by commit,
no match-rate reporting, and — the one that actually mattered — no
concatenating overlapping chunks. The chunker carries up to 500 characters
forward, so 53,030 sections have chunks that overlap; concatenating them
duplicates text, while slicing one contiguous range cannot.

What *is* needed is cleaning. Pre-2019 RFCs are paginated, so a span of any
size carries form feeds, running headers ("RFC 1035  Domain Implementation
and Specification  November 1987") and page footers ("Mockapetris [Page
26]") sitting in the middle of sentences. `clean_section_text` removes them
and rejoins the paragraphs they split.

Two things it deliberately does not do. It does not reflow: RFC bodies are
full of ASCII art (bit diagrams, state tables, message layouts) that only
survives if line structure does, so the only whitespace change is removing
the *common* leading indent, which preserves everything relative. And it
does not detect running headers by repetition the way rfc.fyi's chunker
does — a section slice may cross only one page break, giving nothing to
repeat — so it matches them by shape instead, and a header in neither form
survives as a stray line rather than eating a line of body.

Publisher-side only — see the subpackage docstring.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from .format import ChunkMeta
from .mirror import text_path

#: "Fielding & Reschke        Standards Track           [Page 12]".
#: Same shape rfc.fyi's chunker matches, including roman-numeral pages.
_FOOTER_RE = re.compile(r"^.{0,100}\[\s*Page\s+[0-9ivxlcdmIVXLCDM]+\s*\]\s*$")

#: A running header opens with the RFC number …
_HEADER_RFC_RE = re.compile(r"^\s*RFC[\s-]+\S+\s", re.IGNORECASE)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October"
    "|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
#: … and closes with the publication month and year, after a run of spaces.
_HEADER_DATE_RE = re.compile(rf"\s\s+(?:{_MONTHS})\s+\d{{4}}\s*$")

#: A header line is never long. Body prose that happens to start "RFC 2119"
#: keeps going; a header is a single short banner.
_MAX_HEADER_LEN = 100

#: Terminal punctuation: a line ending in one of these finished its sentence,
#: so whatever follows a page break starts something new.
_SENTENCE_END = (".", ":", "!", "?", ";")


@dataclass(frozen=True)
class SectionSpan:
    """One section's extent in its RFC's source bytes."""

    rfc: str
    section: Optional[str]
    title: str
    start: int
    end: int
    chunks: int

    @property
    def length(self) -> int:
        return self.end - self.start


def section_spans(chunks: Iterable[ChunkMeta]) -> List[SectionSpan]:
    """Collapse chunks into one span per `(rfc, section)`.

    Chunks with no section label (~9% of the corpus, mostly very old
    unnumbered RFCs) are kept individually rather than merged into one
    span-of-everything: without a heading to group them there is nothing
    claiming they are one passage.
    """
    grouped: Dict[Tuple[str, str], List[ChunkMeta]] = {}
    loose: List[SectionSpan] = []
    for chunk in chunks:
        if chunk.section is None:
            loose.append(
                SectionSpan(
                    rfc=chunk.rfc,
                    section=None,
                    title=chunk.title,
                    start=chunk.off,
                    end=chunk.off + chunk.length,
                    chunks=1,
                )
            )
            continue
        grouped.setdefault((chunk.rfc, chunk.section), []).append(chunk)

    spans = [
        SectionSpan(
            rfc=rfc,
            section=section,
            title=members[0].title,
            start=min(c.off for c in members),
            end=max(c.off + c.length for c in members),
            chunks=len(members),
        )
        for (rfc, section), members in grouped.items()
    ]
    spans.extend(loose)
    # Sorted by position so a caller writing them out gets document order,
    # which is also the order a reader expects an outline in.
    spans.sort(key=lambda s: (s.rfc, s.start))
    return spans


def _is_footer(line: str) -> bool:
    return bool(line.strip()) and bool(_FOOTER_RE.match(line))


def _is_running_header(line: str) -> bool:
    if not line.strip() or len(line) > _MAX_HEADER_LEN:
        return False
    if _HEADER_RFC_RE.match(line) and _HEADER_DATE_RE.search(line):
        return True
    # Some RFCs put the title first and the RFC number nowhere on the line;
    # the date tail plus a run of alignment spaces is what gives those away.
    return bool(_HEADER_DATE_RE.search(line) and "  " in line)


def _continues_paragraph(previous: str, following: str) -> bool:
    """Did a page break split one paragraph in two?

    Same test rfc.fyi's chunker uses: the line before the break did not end a
    sentence, and the line after it starts lowercase or with a comma. Both
    halves must agree, so a new paragraph that merely begins lowercase is not
    glued onto the previous page.
    """
    tail, head = previous.rstrip(), following.strip()
    if not tail or not head:
        return False
    if tail.endswith(_SENTENCE_END):
        return False
    return head[0].islower() or head[0] in ",;"


def clean_section_text(raw: str) -> str:
    """Strip page furniture from a section slice and rejoin what it split."""
    kept: List[str] = []
    #: True when everything since the last kept line was page furniture, so
    #: the next real line is the far side of a page break.
    after_break = False
    for line in raw.split("\n"):
        if "\f" in line:
            after_break = True
            # A form feed can share its line with the header that follows it.
            if not line.replace("\f", "").strip():
                continue
            line = line.replace("\f", "")
        if _is_footer(line) or _is_running_header(line):
            after_break = True
            continue
        if after_break and not line.strip():
            # Blank padding around a page break is layout, not structure.
            continue
        if after_break and kept:
            after_break = False
            if _continues_paragraph(kept[-1], line):
                kept[-1] = kept[-1].rstrip() + " " + line.strip()
                continue
        after_break = False
        kept.append(line)

    return _dedent("\n".join(kept)).strip("\n")


def _dedent(text: str) -> str:
    """Remove the common leading indent, and only that.

    RFC bodies are indented three columns; diagrams and tables are indented
    further and their *relative* offsets carry the meaning. Removing the
    common minimum takes the margin off without touching the shape.
    """
    lines = text.split("\n")
    widths = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    if not widths:
        return text
    margin = min(widths)
    if not margin:
        return "\n".join(line.rstrip() for line in lines)
    return "\n".join((line[margin:] if line.strip() else "").rstrip() for line in lines)


def read_section(mirror_dir: str, span: SectionSpan) -> str:
    """Slice `span` out of the mirror and clean it.

    Returns "" when the RFC is not in the mirror — the caller has already
    reconciled digests and knows which those are, so this is a skip rather
    than an error.
    """
    path = text_path(mirror_dir, span.rfc)
    if not os.path.isfile(path):
        return ""
    with open(path, "rb") as fh:
        fh.seek(span.start)
        raw = fh.read(span.length)
    return clean_section_text(raw.decode("utf-8", errors="replace"))
