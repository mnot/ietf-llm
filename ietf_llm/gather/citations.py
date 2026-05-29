"""Cross-reference draft names cited in threads and issues.

Consumer pain point: threads regularly cite Internet-Drafts that the
consumer then has to look up by hand to confirm what's in them. The
reverse direction is just as useful — when reading a draft, knowing
which threads referenced it gives the social context of who's
engaging with the work.

This module scans the per-thread and per-issue markdown files for
`draft-…` references, normalises away the version suffix, and
records `(draft_name → [citing locations])`. The output is rendered
as `digests/citations.md` and consumed by the `overview` document
section (citation counts) and the `find_citations` MCP tool
(per-draft drill-down).

Detection rules:

- Match `draft-<acronym>-…` at word boundaries. Strict enough to
  avoid grabbing `mock-draft-style` substrings; loose enough to
  catch `draft-ietf-foo`, `draft-author-topic`, and the IRTF /
  individual variants.
- Strip trailing `-NN` version suffix on extraction so all revisions
  collapse to one entry. The consumer asking about `draft-foo` cares
  about the draft, not which specific revision someone cited.
- Skip quoted blocks (`> ` prefix). A reply that quotes someone
  else's "see draft-foo" doesn't add a fresh citation — the original
  message already counts.
- Dedupe per (file, chunk_idx) per draft. One message that
  references the same draft three times is one citation, not three.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..paths import digest_path, issues_dir, threads_dir
from ..utils import LogLevel, Verbosity, log


@dataclass
class Citation:
    """One reference to a draft from a thread / issue file."""

    file: str  # path relative to the WG cache root
    chunk_idx: int  # message-section number (1-based) the citation lives in
    context: str  # ~120-char excerpt around the match, single line


# `draft-<acronym>-<segment>(-<segment>)*` with at least two
# hyphen-separated segments after `draft-`. The first segment is the
# acronym ("ietf", an author surname, "irtf"); subsequent segments are
# the topic. The trailing version suffix `-NN` is captured separately
# so we can strip it during normalisation.
_DRAFT_RE = re.compile(
    r"\bdraft-[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b",
    re.IGNORECASE,
)
_VERSION_SUFFIX_RE = re.compile(r"-\d{2}(?:\.txt)?$|\.txt$")

# Per-thread / per-issue section header — `### [N] DATE — Sender`.
# Used to map a citation's offset to the message it lives in.
_SECTION_RE = re.compile(r"^### \[(\d+)\]", re.MULTILINE)


def normalize_draft_name(raw: str) -> str:
    """Lowercase + strip version suffix and `.txt` extension.

    `draft-Foo-Bar-07`       → `draft-foo-bar`
    `draft-foo-bar-07.txt`   → `draft-foo-bar`
    `draft-foo-bar.txt`      → `draft-foo-bar`
    `draft-foo-bar`          → `draft-foo-bar`
    """
    return _VERSION_SUFFIX_RE.sub("", raw.strip().lower())


def _strip_quoted(text: str) -> str:
    """Drop quoted lines so we don't count forwarded citations.

    Per-thread files have inline `> quoted` content where someone
    quoted an earlier message; counting `draft-foo` in those would
    double-count the original.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )


def _chunk_for_offset(sections: List[re.Match[str]], offset: int) -> int:
    """Given the section-header matches for a file, return the
    chunk_idx (= section number) the given byte offset falls in.
    Returns 0 for offsets before the first section (i.e. in the
    file's outline / header area).
    """
    chunk_idx = 0
    for section in sections:
        if section.start() > offset:
            break
        chunk_idx = int(section.group(1))
    return chunk_idx


def _scan_file(path: str, relpath: str) -> List[tuple[str, Citation]]:
    """Scan one thread / issue file for draft citations. Returns
    `(draft_name, Citation)` pairs in document order, deduped per
    (file, chunk_idx, draft)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []
    cleaned = _strip_quoted(text)
    sections = list(_SECTION_RE.finditer(cleaned))
    seen: set[tuple[str, int, str]] = set()
    out: List[tuple[str, Citation]] = []
    for match in _DRAFT_RE.finditer(cleaned):
        raw = match.group(0)
        # Reject obvious false positives. A bare "draft-" with no
        # acronym-and-topic structure (`draft-foo`) should still
        # match — the regex enforces at least two hyphen-separated
        # segments after `draft-`. But a too-long token (>60 chars)
        # probably ran into adjacent text.
        if len(raw) > 60:
            continue
        draft = normalize_draft_name(raw)
        chunk_idx = _chunk_for_offset(sections, match.start())
        key = (relpath, chunk_idx, draft)
        if key in seen:
            continue
        seen.add(key)
        # ~120-char window for context. Collapse internal newlines so
        # the excerpt renders on one line in the digest.
        start = max(0, match.start() - 40)
        end = min(len(cleaned), match.end() + 80)
        context = " ".join(cleaned[start:end].split())
        out.append(
            (
                draft,
                Citation(
                    file=relpath,
                    chunk_idx=chunk_idx,
                    context=context,
                ),
            ),
        )
    return out


def scan_citations(
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> Dict[str, List[Citation]]:
    """Walk threads/ and issues/ in a WG cache. Return draft → list
    of citations (in stable order by file + chunk_idx).
    """
    out: Dict[str, List[Citation]] = {}
    n_files = 0
    for root_dir in (threads_dir(cache_dir), issues_dir(cache_dir)):
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                relpath = os.path.relpath(path, cache_dir)
                n_files += 1
                for draft, citation in _scan_file(path, relpath):
                    out.setdefault(draft, []).append(citation)
    # Sort each draft's citations by file then chunk_idx for stable
    # output across gathers.
    for entries in out.values():
        entries.sort(key=lambda c: (c.file, c.chunk_idx))
    log(
        f"Scanned {n_files} thread / issue file(s); "
        f"found {len(out)} distinct draft citation(s)",
        verbose,
        level=LogLevel.STATUS,
    )
    return out


def write_citations_digest(
    cache_dir: str,
    citations: Dict[str, List[Citation]],
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Render `digests/citations.md`. Returns the path, or None if
    no citations were found."""
    if not citations:
        return None
    out_path = digest_path(cache_dir, "citations")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("# Draft citations\n\n")
        fh.write(
            f"_{len(citations)} distinct draft(s) cited across "
            "this WG's threads and issues. Each entry lists the "
            "files (and message / comment chunks) that reference "
            "the draft — useful for jumping from a draft to the "
            "context that discusses it, or the other way around. "
            "Quoted blocks are excluded so a forward doesn't "
            "double-count its source._\n\n"
        )
        for draft in sorted(citations):
            entries = citations[draft]
            # Use "N citations" rather than "N citation(s)" — nested
            # parens in the header confused the regex consumers
            # (overview's count parser, find_citations' section
            # parser). One word is also less noisy.
            noun = "citation" if len(entries) == 1 else "citations"
            fh.write(f"## `{draft}` ({len(entries)} {noun})\n\n")
            for citation in entries:
                fh.write(
                    f"- `{citation.file}` [chunk {citation.chunk_idx}] "
                    f"— _{citation.context}_\n"
                )
            fh.write("\n")
    log(
        f"Wrote citations digest: {len(citations)} draft(s)",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


def citation_counts(citations: Dict[str, List[Citation]]) -> Dict[str, int]:
    """Convenience: collapse to `{draft_name: count}`. Used by
    `overview` to annotate documents with their citation count
    without needing to walk the full list."""
    return {draft: len(entries) for draft, entries in citations.items()}
