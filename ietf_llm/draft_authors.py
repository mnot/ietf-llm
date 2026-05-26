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


@dataclass
class DraftAuthor:
    name: str
    email: Optional[str]
    is_editor: bool = False


# Match both "Author's Address" (singular possessive) and
# "Authors' Address[es]" (plural possessive) — RFC and I-D layouts use
# both depending on whether the doc has one author or several. The
# `(?:es)?` form (rather than the more natural `Addresses?`) is
# deliberate: stacking `?` quantifiers on adjacent tokens before `\s*$`
# produces a backtracking pathology in Python's `re` that silently
# fails to match. Using an explicit non-capturing group avoids it.
_HEADER_RE = re.compile(
    r"^Author(?:'s|s')?\s+Address(?:es)?\s*$", re.MULTILINE
)
_PAGE_FOOTER_RE = re.compile(r"\[Page\s+\d+\]|Expires?\s+\w+\s+\d{4}")
_EDITOR_SUFFIX_RE = re.compile(
    r"^(.+?)\s*\((?:editor|ed\.?)\)\s*$", re.IGNORECASE
)
_EMAIL_LINE_RE = re.compile(r"^Email:\s*(\S+@\S+)", re.IGNORECASE)


def parse_authors(text: str) -> List[DraftAuthor]:
    """Extract `DraftAuthor` records from the Authors' Addresses tail."""
    match = _HEADER_RE.search(text)
    if not match:
        return []

    tail = text[match.end() :]
    authors: List[DraftAuthor] = []
    block: List[str] = []
    for raw in tail.splitlines():
        # Form-feed / page boundaries — keep scanning across pages.
        if raw.startswith("\f") or _PAGE_FOOTER_RE.search(raw):
            continue
        # The next major section heading (rare in RFCs, more common in
        # earlier I-Ds with appendices after addresses).
        if raw and not raw.startswith(" ") and not raw.startswith("\t"):
            if block:
                _commit_block(block, authors)
                block = []
            # A non-indented, non-blank line at this point is a section
            # boundary; stop.
            break
        stripped = raw.strip()
        if not stripped:
            if block:
                _commit_block(block, authors)
                block = []
            continue
        block.append(stripped)
    if block:
        _commit_block(block, authors)
    return authors


def _commit_block(lines: List[str], out: List[DraftAuthor]) -> None:
    """Pull (name, email, is_editor) from one indented block."""
    name: Optional[str] = None
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
    if name:
        out.append(DraftAuthor(name=name, email=email, is_editor=is_editor))


# --- WG-cache walker -------------------------------------------------------


_DRAFT_FILE_RE = re.compile(
    r"^(?P<base>(?:draft|rfc)-[^/]+?)-(?P<version>\d+)\.txt$"
)


def latest_draft_paths(cache_dir: str) -> List[str]:
    """Return one path per draft (the highest-numbered version), plus RFCs.

    `draft-ietf-aipref-vocab-{00..06}.txt` collapses to just the -06
    file. RFCs (`rfc1234.txt`) appear unchanged. Non-draft files are
    ignored.
    """
    by_base: dict[str, "tuple[int, str]"] = {}
    extras: List[str] = []
    if not os.path.isdir(cache_dir):
        return []
    for name in sorted(os.listdir(cache_dir)):
        lower = name.lower()
        if not lower.endswith(".txt"):
            continue
        if lower.startswith("rfc"):
            extras.append(os.path.join(cache_dir, name))
            continue
        match = _DRAFT_FILE_RE.match(lower)
        if not match:
            continue
        version = int(match.group("version"))
        base = match.group("base")
        prev = by_base.get(base)
        if prev is None or prev[0] < version:
            by_base[base] = (version, os.path.join(cache_dir, name))
    return [path for _, path in by_base.values()] + extras
