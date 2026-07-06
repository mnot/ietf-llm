"""Cross-reference archive-permalink URLs cited between messages.

Consumer pain point: a message routinely cites an *earlier message* by
its mail-archive URL — an appeal links the post being appealed, a split
thread links the message it forked from, a reply footnotes the message
it answers. When that target is itself gathered (same corpus, often the
same thread cluster under an earlier subject), the content is already in
the corpus — but from a bare URL the consumer has no cheap way to see
that, and ends up either skipping it or wrongly concluding it "isn't in
the corpus". `get_by_url` resolves a single URL on demand; this module
builds the *graph* so the reverse direction ("who cites this message?")
and the cross-corpus references are visible at a glance.

This is the message-level analogue of `citations.py` (which does the
same for `draft-…` references). It scans the per-thread and per-issue
markdown files for `mailarchive.ietf.org/arch/msg/…` and
`www.w3.org/mid/…` URLs, resolves each against the `_Archived-At:_`
permalinks stamped on the gathered messages, and renders
`digests/message_citations.md`, consumed by the `find_message_citations`
MCP tool.

Detection rules:

- Two list-archive flavours: IETF mailarchive (an opaque token) and the
  W3C `mid` redirector (an RFC 5322 Message-ID). Both appear as
  `_Archived-At:_` lines *and* inline in bodies.
- Resolution is *within-scheme* (trailing slash / scheme / `www.`): a
  `mailarchive` token is NOT mapped to a `w3.org/mid` Message-ID — the
  two are different identifiers for the same message and not
  string-convertible. A body citing the opposite scheme from the one a
  list stamps therefore lands in "External" even when the target is
  technically gathered. Documented limitation, same as `get_by_url`.
- Skip quoted blocks (`> ` prefix) and each message's own
  `_Archived-At:_` line, so a message neither cites itself nor
  double-counts a URL it merely quoted.
- Dedupe per (file, chunk_idx, url): one message linking the same
  target three times is one citation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ...atomicio import atomic_open
from ...log import LogLevel, Verbosity, log
from ...paths import (
    digest_path,
    iter_thread_issue_md_files,
    remove_stale_digest,
    threads_dir,
)

# Archive permalinks, as they appear both on `_Archived-At:_` lines (the
# resolver index) and inline in bodies (the citations we scan). Trailing
# sentence / footnote punctuation is trimmed by `_trim_url`.
_ARCHIVE_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:mailarchive\.ietf\.org/arch/msg|w3\.org/mid)/\S+",
    re.IGNORECASE,
)

# Per-thread / per-issue section header — `### [N] DATE — Sender`.
_SECTION_RE = re.compile(r"^### \[(\d+)\]", re.MULTILINE)
# Same, but capturing date (group 2) and sender (group 3) for display.
_SECTION_HEADER_RE = re.compile(
    r"^### \[(\d+)\] (\S+(?:\s+\S+)?) — (.+?)$", re.MULTILINE
)
_ARCHIVED_AT_RE = re.compile(r"^_Archived-At:_\s*(\S+)", re.MULTILINE)
_SUBJECT_RE = re.compile(r"^_Subject:_\s*(.+?)\s*$", re.MULTILINE)
# Trailing "(reply to [N])" the thread renderer appends to a sender.
_REPLY_TO_RE = re.compile(r"\s*\(reply to \[\d+\]\)\s*$")
# Mailarchive list shortname: `/arch/msg/<list>/<token>`.
_LIST_FROM_URL_RE = re.compile(r"/arch/msg/([^/]+)/", re.IGNORECASE)


@dataclass
class MessageRef:
    """A gathered message — the resolution target of an archive URL."""

    file: str  # path relative to the WG cache root
    chunk_idx: int  # message-section number (1-based)
    sender: str
    date: str
    subject: str


@dataclass
class MessageCitation:
    """One archive-URL reference found in a message body."""

    src_file: str
    src_chunk_idx: int
    url: str  # canonicalised
    context: str  # ~120-char excerpt around the match, single line
    target: Optional[MessageRef]  # None → external / not gathered here


def _trim_url(url: str) -> str:
    """Strip trailing punctuation / brackets a sentence wraps a URL in."""
    return url.strip().strip("<>").rstrip(").,;'\"’”]")


def canonical_archive_url(url: str) -> str:
    """Within-scheme canonical key for matching a cited URL to a stored
    `Archived-At:` permalink.

    Normalises scheme (→ https), a leading `www.`, a trailing slash, and
    drops a `#fragment`. Host is lowercased; the *path* is left as-is
    because a `w3.org/mid` Message-ID is case-sensitive. Mirrors the
    tolerance of `find_chunks_by_url`, but never bridges schemes.
    """
    text = _trim_url(url).split("#", 1)[0]
    match = re.match(r"^(https?)://([^/]+)(.*)$", text, re.IGNORECASE)
    if not match:
        return text
    host = match.group(2).lower()
    if host.startswith("www."):
        host = host[4:]
    path = match.group(3).rstrip("/")
    return f"https://{host}{path}"


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _clean_sender(raw: str) -> str:
    return _REPLY_TO_RE.sub("", raw).strip()


def _strip_for_scan(text: str) -> str:
    """Drop quoted lines and `_Archived-At:_` lines before scanning.

    Quoted content (`> …`) would double-count a forwarded link; a
    message's own `_Archived-At:_` line would make it cite itself.
    Section headers are left intact so offset→chunk mapping still works.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            out.append("")
            continue
        if stripped.startswith("_Archived-At:_"):
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def _chunk_for_offset(sections: List["re.Match[str]"], offset: int) -> int:
    """chunk_idx (= section number) the given byte offset falls in; 0 if
    it precedes the first section (the file's outline / header area)."""
    chunk_idx = 0
    for section in sections:
        if section.start() > offset:
            break
        chunk_idx = int(section.group(1))
    return chunk_idx


def build_resolver(cache_dir: str) -> Dict[str, MessageRef]:
    """Map `canonical_archive_url(Archived-At) → MessageRef` for every
    gathered thread message. Only threads carry `_Archived-At:_` lines;
    issues have no per-comment permalink, so they are citation sources
    but never resolution targets.
    """
    index: Dict[str, MessageRef] = {}
    tdir = threads_dir(cache_dir)
    if not os.path.isdir(tdir):
        return index
    for dirpath, _dirnames, filenames in os.walk(tdir):
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            relpath = os.path.relpath(path, cache_dir)
            text = _read(path)
            sections = list(_SECTION_HEADER_RE.finditer(text))
            for i, sec in enumerate(sections):
                end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
                block = text[sec.start() : end]
                archived = _ARCHIVED_AT_RE.search(block)
                if not archived:
                    continue
                subject = _SUBJECT_RE.search(block)
                index[canonical_archive_url(archived.group(1))] = MessageRef(
                    file=relpath,
                    chunk_idx=int(sec.group(1)),
                    sender=_clean_sender(sec.group(3)),
                    date=sec.group(2),
                    subject=subject.group(1).strip() if subject else "",
                )
    return index


def _scan_file(
    path: str, relpath: str, resolver: Dict[str, MessageRef]
) -> List[MessageCitation]:
    """Scan one thread / issue file for archive-URL citations, deduped
    per (file, chunk_idx, url). A URL that resolves to the *same*
    message it appears in is dropped (a stray self-reference)."""
    cleaned = _strip_for_scan(_read(path))
    sections = list(_SECTION_RE.finditer(cleaned))
    seen: set[Tuple[str, int, str]] = set()
    out: List[MessageCitation] = []
    for match in _ARCHIVE_URL_RE.finditer(cleaned):
        url = canonical_archive_url(match.group(0))
        chunk_idx = _chunk_for_offset(sections, match.start())
        key = (relpath, chunk_idx, url)
        if key in seen:
            continue
        seen.add(key)
        target = resolver.get(url)
        if target and target.file == relpath and target.chunk_idx == chunk_idx:
            continue
        start = max(0, match.start() - 40)
        end = min(len(cleaned), match.end() + 80)
        out.append(
            MessageCitation(
                src_file=relpath,
                src_chunk_idx=chunk_idx,
                url=url,
                context=" ".join(cleaned[start:end].split()),
                target=target,
            )
        )
    return out


def scan_message_citations(
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[MessageCitation]:
    """Walk threads/ and issues/, resolving every archive-URL citation
    against the gathered messages. Returns the flat citation list in
    stable order (by source file, then chunk)."""
    resolver = build_resolver(cache_dir)
    out: List[MessageCitation] = []
    n_files = 0
    for path, relpath in iter_thread_issue_md_files(cache_dir):
        n_files += 1
        out.extend(_scan_file(path, relpath, resolver))
    out.sort(key=lambda c: (c.src_file, c.src_chunk_idx, c.url))
    n_resolved = sum(1 for c in out if c.target)
    log(
        f"Scanned {n_files} thread / issue file(s); found {len(out)} "
        f"message citation(s) ({n_resolved} resolved, "
        f"{len(out) - n_resolved} external)",
        verbose,
        level=LogLevel.STATUS,
    )
    return out


def _list_hint(url: str) -> str:
    """`(gather `<list>`?)` hint for an unresolved mailarchive URL, or ""
    when the list can't be read off the URL (e.g. a w3.org/mid link)."""
    match = _LIST_FROM_URL_RE.search(url)
    return f"  (gather `{match.group(1)}`?)" if match else ""


def write_message_citations_digest(
    cache_dir: str,
    citations: List[MessageCitation],
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Render `digests/message_citations.md`, grouped by cited target
    (the reverse "who references this message" index) plus an External
    section for URLs not gathered in this corpus. Returns the path, or
    None if there were no citations.

    On empty, drop any digest left by an earlier gather (see
    `remove_stale_digest`) so `find_message_citations` reports "no
    digest" rather than stale citation edges.
    """
    if not citations:
        remove_stale_digest(cache_dir, "message_citations")
        return None

    by_target: Dict[Tuple[str, int], List[MessageCitation]] = {}
    targets: Dict[Tuple[str, int], MessageRef] = {}
    external: Dict[str, List[MessageCitation]] = {}
    for cit in citations:
        if cit.target is not None:
            key = (cit.target.file, cit.target.chunk_idx)
            by_target.setdefault(key, []).append(cit)
            targets[key] = cit.target
        else:
            external.setdefault(cit.url, []).append(cit)

    out_path = digest_path(cache_dir, "message_citations")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with atomic_open(out_path) as fh:
        fh.write("# Message citations\n\n")
        fh.write(
            f"_{len(by_target)} gathered message(s) referenced by other "
            f"messages, and {len(external)} reference(s) to messages not "
            "gathered in this corpus. Each entry maps an archive permalink "
            "cited in a body back to the message it points at — useful for "
            "walking the reference graph of a discussion (appeals, split "
            "threads, replies-by-link) instead of treating each thread file "
            "as an island. Quoted blocks and a message's own Archived-At "
            "line are excluded._\n\n"
        )
        if by_target:
            fh.write("## Resolved (target gathered here)\n\n")
            for key in sorted(by_target):
                ref = targets[key]
                subject = f' — "{ref.subject}"' if ref.subject else ""
                fh.write(
                    f"### `{ref.file}` [chunk {ref.chunk_idx}] — "
                    f"{ref.sender}, {ref.date}{subject}\n\n"
                )
                fh.write("cited by:\n\n")
                for cit in sorted(
                    by_target[key], key=lambda c: (c.src_file, c.src_chunk_idx)
                ):
                    fh.write(
                        f"- `{cit.src_file}` [chunk {cit.src_chunk_idx}] "
                        f"— _{cit.context}_\n"
                    )
                fh.write("\n")
        if external:
            fh.write("## External (not gathered in this corpus)\n\n")
            for url in sorted(external):
                fh.write(f"### {url}{_list_hint(url)}\n\n")
                fh.write("cited by:\n\n")
                for cit in sorted(
                    external[url], key=lambda c: (c.src_file, c.src_chunk_idx)
                ):
                    fh.write(
                        f"- `{cit.src_file}` [chunk {cit.src_chunk_idx}] "
                        f"— _{cit.context}_\n"
                    )
                fh.write("\n")
    log(
        f"Wrote message-citations digest: {len(by_target)} resolved "
        f"target(s), {len(external)} external",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


def build_message_citations(
    cache_dir: str,
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Scan the corpus and render `digests/message_citations.md` in one
    call (the gather entry point). Returns the digest path, or None when
    no archive-URL citations were found."""
    return write_message_citations_digest(
        cache_dir, scan_message_citations(cache_dir, verbose=verbose), verbose=verbose
    )
