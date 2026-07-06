"""Parse the Authors' Addresses section of IETF drafts and RFCs.

The text format is stable across decades: a trailing section headed
"Authors' Addresses" (or just "Author's Address" for solo-authored
drafts) containing one indented block per author. Each block is
roughly:

    <Name> [(editor)]
    <Affiliation>
    Email: <address>
    URI:   <url>          # optional

We tolerate the variations — missing affiliation, multiple Email
lines, page-footer interruptions, the (ed.) abbreviation, RFC vs
I-D layout — but bail out gracefully if a draft doesn't conform.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from ...paths import drafts_dir


@dataclass
class DraftAuthor:
    name: str
    email: Optional[str]
    is_editor: bool = False
    # Author's stated organisation, taken from the line immediately
    # after the name in the Authors' Addresses block. Free-form text
    # (e.g. "Cloudflare", "Independent", "Mozilla") — author-written,
    # so a person can ship different orgs across different drafts.
    # Often missing (line skipped entirely) when an author works as
    # an individual; surface as None in that case rather than forcing
    # a placeholder.
    organization: Optional[str] = None


# Match both "Author's Address" (singular possessive) and
# "Authors' Address[es]" (plural possessive) — RFC and I-D layouts use
# both depending on whether the doc has one author or several. The
# `(?:es)?` form (rather than the more natural `Addresses?`) is
# deliberate: stacking `?` quantifiers on adjacent tokens before `\s*$`
# produces a backtracking pathology in Python's `re` that silently
# fails to match. Using an explicit non-capturing group avoids it.
_HEADER_RE = re.compile(r"^Author(?:'s|s')?\s+Address(?:es)?\s*$", re.MULTILINE)
_PAGE_FOOTER_RE = re.compile(r"\[Page\s+\d+\]|Expires?\s+\w+\s+\d{4}")
_EDITOR_SUFFIX_RE = re.compile(r"^(.+?)\s*\((?:editor|ed\.?)\)\s*$", re.IGNORECASE)
_EMAIL_LINE_RE = re.compile(r"^Email:\s*(\S+@\S+)", re.IGNORECASE)
# A trailing-colon label rather than a name — e.g. the "Additional contact
# information:" sub-heading some drafts place inside an author block to
# introduce the author's name in their native script. Real names and
# organisations never end in a colon, so this is safe to treat as a marker.
_LABEL_LINE_RE = re.compile(r"^.*\S:\s*$")


def parse_authors(text: str) -> List[DraftAuthor]:
    """Extract `DraftAuthor` records from the Authors' Addresses tail."""
    match = _HEADER_RE.search(text)
    if not match:
        return []

    tail = text[match.end() :]
    authors: List[DraftAuthor] = []
    block: List[str] = []
    block_indent = 0
    # When an "Additional contact information:" sub-heading appears, its
    # body is indented one level deeper than the surrounding author blocks
    # and belongs to the author above it — not a new person. Remember that
    # label's indent here and skip any block nested deeper than it until
    # indentation returns to the author level.
    skip_below: Optional[int] = None
    for raw in tail.splitlines():
        # Form-feed / page boundaries — keep scanning across pages.
        if raw.startswith("\f") or _PAGE_FOOTER_RE.search(raw):
            continue
        # The next major section heading (rare in RFCs, more common in
        # earlier I-Ds with appendices after addresses).
        if raw and not raw.startswith(" ") and not raw.startswith("\t"):
            skip_below = _flush_block(block, block_indent, skip_below, authors)
            block = []
            # A non-indented, non-blank line at this point is a section
            # boundary; stop.
            break
        stripped = raw.strip()
        if not stripped:
            skip_below = _flush_block(block, block_indent, skip_below, authors)
            block = []
            continue
        if not block:
            block_indent = len(raw) - len(raw.lstrip())
        block.append(stripped)
    _flush_block(block, block_indent, skip_below, authors)
    return authors


def _flush_block(
    lines: List[str],
    indent: int,
    skip_below: Optional[int],
    out: List[DraftAuthor],
) -> Optional[int]:
    """Commit one block as an author, or skip it as additional contact info.

    Returns the updated `skip_below` threshold (see `parse_authors`). A
    block is skipped — rather than turned into a `DraftAuthor` — when it is
    either an "Additional contact information:" label or a block nested
    deeper than such a label (the label's body, which restates the
    preceding author in another script).
    """
    if not lines:
        return skip_below
    if skip_below is not None and indent > skip_below:
        # Deeper-indented body of an "Additional contact information:"
        # group — it belongs to the author above, so drop it.
        return skip_below
    if _LABEL_LINE_RE.match(lines[0]):
        # The label itself: skip it and anything indented beneath it.
        return indent
    _commit_block(lines, out)
    return None


def _commit_block(lines: List[str], out: List[DraftAuthor]) -> None:
    """Pull (name, organization, email, is_editor) from one indented block.

    Block layout (RFC / I-D convention):

        Mark Nottingham (editor)        ← name (optional editor suffix)
        Cloudflare                      ← organization (optional)
        Email: mnot@mnot.net            ← contact line
        URI:   https://www.mnot.net/    ← skipped

    The organisation is whatever non-metadata line appears between the
    name and the first `Email:`. We only keep one organisation per
    block — drafts sometimes wrap addresses across multiple lines but
    we don't treat trailing address lines (street, city) as the org.
    """
    name: Optional[str] = None
    organization: Optional[str] = None
    email: Optional[str] = None
    is_editor = False
    for line in lines:
        em_match = _EMAIL_LINE_RE.match(line)
        if em_match:
            if email is None:
                email = em_match.group(1).strip().rstrip(".,")
            continue
        # Skip the other RFC-format metadata lines.
        if line.startswith(("Phone:", "URI:", "Fax:", "Tel:")):
            continue
        if name is None:
            ed_match = _EDITOR_SUFFIX_RE.match(line)
            if ed_match:
                name = ed_match.group(1).strip()
                is_editor = True
            else:
                name = line
        elif organization is None:
            # First non-metadata, non-name line is the org. Drafts
            # vary: some authors include only an org, some include
            # mailing address lines below that. Take the first; it's
            # the one that matters for affiliation surfacing.
            organization = line
    if name:
        out.append(
            DraftAuthor(
                name=name,
                email=email,
                is_editor=is_editor,
                organization=organization,
            )
        )


# --- WG-cache walker -------------------------------------------------------


_DRAFT_FILE_RE = re.compile(r"^(?P<base>(?:draft|rfc)-[^/]+?)-(?P<version>\d+)\.txt$")


def latest_draft_paths(cache_dir: str) -> List[str]:
    """Return one path per draft (the highest-numbered version), plus RFCs.

    `draft-ietf-aipref-vocab-{00..06}.txt` collapses to just the -06
    file. RFCs (`rfc1234.txt`) appear unchanged. Looks in `drafts/`
    (post-reorg) and falls back to the cache root for older layouts.
    """
    by_base: dict[str, "tuple[int, str]"] = {}
    extras: List[str] = []
    if not os.path.isdir(cache_dir):
        return []
    scan_dir = drafts_dir(cache_dir)
    if not os.path.isdir(scan_dir):
        return []
    for name in sorted(os.listdir(scan_dir)):
        lower = name.lower()
        if not lower.endswith(".txt"):
            continue
        if lower.startswith("rfc"):
            extras.append(os.path.join(scan_dir, name))
            continue
        match = _DRAFT_FILE_RE.match(lower)
        if not match:
            continue
        version = int(match.group("version"))
        base = match.group("base")
        prev = by_base.get(base)
        if prev is None or prev[0] < version:
            by_base[base] = (version, os.path.join(scan_dir, name))
    return [path for _, path in by_base.values()] + extras
