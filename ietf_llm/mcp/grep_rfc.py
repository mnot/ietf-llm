"""The RFC-series backend of `grep_corpus` (`corpus="rfcs"`).

`grep.tool_grep_corpus` dispatches here; the differences a caller sees from
the gathered-corpus scan are set out above `_grep_rfc_corpus`.
"""

from __future__ import annotations

import fnmatch
import re
import time
from typing import Dict, List, NamedTuple, Optional, Tuple, cast

from ..embeddings.storage import _db_path_ro, indexed_files, iter_sections
from ..regexscan import (
    ScanFailed,
    ScanTimedOut,
    SectionScan,
    run_bounded,
    scan_sections,
)
from .grep_common import (
    _MAX_GREP_LIMIT,
    _SCAN_FAILED_REPLY,
    _incomplete_scan_head,
    _mode_phrase,
    _note_scan_stop,
)
from .rfc_text import RFC_CORPUS, provenance_line, status_note

# --- the RFC series ----------------------------------------------------------
#
# The RFC corpus is not a gathered corpus: it has no `files/` dir, only an
# `embeddings.db` installed by `ietf-llm --init`. The plain-text mirror this
# would otherwise scan (`_rfc/text/`) is publisher-side and absent on a client,
# so the chunk table *is* the text on every machine that has the corpus at all
# — and scanning it is the only implementation that works for the people who
# installed it rather than built it.
#
# Two consequences the caller sees. Hits are located as **RFC + section**, not
# file + line: the index carries rfc.fyi's byte offsets, and `start_line` is
# NULL for every row, so a line number does not exist to report. That is the
# better citation unit anyway — a section is what `get_rfc_section` takes.
# And a literal pattern matches **across line breaks**, because RFC text is
# hard-wrapped at 72 columns: line-bounded matching would miss any phrase
# longer than about ten words, which is most of what anyone asks this.


#: Sections rendered per RFC, and matches rendered per section. A broad
#: pattern hits thousands of sections; these keep one call readable while the
#: reported totals stay complete.
_RFC_SECTIONS_PER_DOC = 3
_RFC_MATCHES_PER_SECTION = 2

#: Wall-clock budget for one RFC-series scan. A literal phrase crosses the
#: whole corpus in ~2.5s, but a plausible regex is far slower — `[A-Za-z]+ing`
#: takes ~25s and `(\s|\w)+$` ~88s over the same ~400 MB, and nothing about
#: either is adversarial. Stopping and *saying so* is the only safe behaviour:
#: a truncated scan that rendered as a normal result would be the unsound
#: negative this tool exists to prevent. A regex runs in a child process
#: (`regexscan`) that stops itself at the budget and is killed shortly after
#: if one `finditer` over a single section never returns.
_RFC_SCAN_BUDGET_S = 20.0

#: How often the budget is polled, in sections. A clock read per section would
#: be ~264k of them for no gain; this is a small fraction of the corpus either
#: way.
_RFC_SCAN_CHECK_EVERY = 512


def _rfc_number(file: str) -> str:
    """`rfc9111.txt` → `9111`. The corpus filename is the only identifier the
    chunk rows carry."""
    return file[3:-4] if file.startswith("rfc") and file.endswith(".txt") else file


def _compile_phrase(
    pattern: str, regex: bool, case_sensitive: bool
) -> "re.Pattern[str]":
    """Compile a pattern for matching against assembled section text.

    A literal pattern has every run of whitespace turned into "any whitespace",
    so a phrase matches regardless of where the publisher's 72-column wrap fell
    in the middle of it. That is the whole point of scanning sections rather
    than lines, and it is why this cannot reuse `_compile`.

    A regex is the caller's own and is passed through untouched, under
    MULTILINE so `^` and `$` still mean line edges within the section.
    """
    flags = re.MULTILINE if case_sensitive else re.MULTILINE | re.IGNORECASE
    if regex:
        return re.compile(pattern, flags)
    return re.compile(r"\s+".join(re.escape(word) for word in pattern.split()), flags)


class _RfcHit(NamedTuple):
    """One matching section of one RFC."""

    number: str
    section: Optional[str]
    title: str
    #: Not `count` — that is `tuple.count`, and shadowing it here is a type
    #: error rather than a style question.
    matches: int
    excerpts: List[List[str]]


def _rfc_scope_phrase(file_pattern: Optional[str], docs: int, sections: int) -> str:
    scope = f"`{file_pattern}`" if file_pattern else "the whole series"
    return f"scanned {docs} RFC(s) / {sections} section(s) — {scope}"


def _rfc_no_match_body(
    pattern: str, file_pattern: Optional[str], mode: str, docs: int, sections: int
) -> str:
    """A zero over the whole series is a strong claim, so it states its own
    scope and its own two remaining limits."""
    lines = [
        f"(no matches for `{pattern}` in the RFC series — {mode}; "
        f"{_rfc_scope_phrase(file_pattern, docs, sections)}.)",
        "",
        "_This is a complete scan of the indexed text of every RFC above, so "
        "it is real evidence of absence — unlike a semantic miss. Two limits "
        "bound the claim. **Front and back matter is not indexed** (status of "
        "this memo, copyright, authors' addresses, the reference list), so a "
        "string that appears only there will not be found. And the corpus is "
        "a dated snapshot — the provenance line below says which — so an RFC "
        "published since is not in it._",
    ]
    if file_pattern:
        lines.append(
            f"\n_Scoped to `{file_pattern}` — re-run without `file_pattern` "
            "before treating this as absence from the series._"
        )
    return "\n".join(lines)


def _rfc_footer() -> str:
    return (
        "\n_Next: read a hit in full with "
        '`get_rfc_section(rfc="<number>", section="<section>")` — the '
        "excerpts above are windows, not the section. `search_rfc_text` for "
        "the semantic view of the same corpus._"
    )


def _render_rfc_files_only(
    per_doc: Dict[str, int], total: int, header: str, limit: int
) -> str:
    ranked = sorted(per_doc.items(), key=lambda kv: (-kv[1], _int_or_zero(kv[0])))
    shown = ranked[:limit]
    out = [header, ""]
    for number, count in shown:
        out.append(f"{count:>6} match(es)  RFC {number}{status_note(number)}")
    if len(ranked) > len(shown):
        out.append(
            f"\n_{len(ranked) - len(shown)} more RFC(s) matched "
            f"(showing {len(shown)} of {len(ranked)}); raise `limit`._"
        )
    out.append(
        f"\n_{total} match(es) across {len(ranked)} RFC(s). "
        "Drop `files_only` to see the passages themselves._"
    )
    out.append(_rfc_footer())
    return "\n".join(out)


def _int_or_zero(value: str) -> int:
    """Sort key for an RFC number that may carry a trailing letter (`17a`)."""
    digits = "".join(c for c in value if c.isdigit())
    return int(digits) if digits else 0


def _render_rfc_hits(
    hits: List[_RfcHit], matching_sections: int, header: str, limit: int
) -> str:
    """`matching_sections` is the true count from the scan, not `len(hits)` —
    which is a retention artefact, capped both overall and per RFC, and so
    cannot be the denominator in a sentence claiming the counts are complete."""
    out = [header, ""]
    current: Optional[str] = None
    shown = 0
    for hit in hits:
        if shown >= limit:
            break
        if hit.number != current:
            current = hit.number
            out.append(f"## RFC {hit.number}{status_note(hit.number)}")
        label = f"§{hit.section}" if hit.section else "(unsectioned)"
        suffix = f" ({hit.matches} matches)" if hit.matches > 1 else ""
        out.append(f"- **{label} {hit.title}**{suffix}")
        for block in hit.excerpts:
            for line in block:
                out.append(f"      {line}")
            out.append("")
        shown += 1
    if matching_sections > shown:
        out.append(
            f"_Showing {shown} of {matching_sections} matching section(s), at "
            f"most {_RFC_SECTIONS_PER_DOC} per RFC — the totals above are the "
            "complete scan, only this listing is capped. Raise `limit` "
            f"(max {_MAX_GREP_LIMIT}), narrow with `file_pattern`, or pass "
            "`files_only=True` for one row per RFC._"
        )
    out.append(_rfc_footer())
    return "\n".join(out)


def _rfc_scan_scope(
    file_pattern: Optional[str],
) -> Tuple[List[str], Optional[str]]:
    """The RFCs a scan will cover, or a message saying why it will not run."""
    files = indexed_files(RFC_CORPUS)
    if not files:
        return [], (
            "The RFC full-text corpus is not installed on this server, so "
            "there is nothing to scan — `ietf-llm --init` installs it. "
            "`search_rfc_index` searches titles and keywords either way."
        )
    if not file_pattern:
        return files, None
    narrowed = [f for f in files if fnmatch.fnmatch(f, file_pattern)]
    if not narrowed:
        return [], (
            f"(no RFC matches `{file_pattern}`, so nothing was scanned — "
            "this is **not** evidence of absence. The scan globs the "
            "corpus filename, which is `rfc9111.txt`, so `rfc91*` works "
            "and `9111` does not.)"
        )
    return narrowed, None


_SECTION_SCAN_KEYS = (
    "sections",
    "total",
    "matching_sections",
    "per_file",
    "hits",
    "stopped_early",
    "error",
)


def _run_scan(
    matcher: "re.Pattern[str]",
    regex: bool,
    scoped: Optional[List[str]],
    keep: int,
    context: int,
) -> SectionScan:
    """Scan the series for `matcher`. The caller's own regex runs in a child
    process, killed at the budget (`regexscan`); a literal phrase is `\\s+`-joined
    words and cannot backtrack catastrophically, so it scans here — still with
    the soft budget, since 400 MB is slow even when the pattern is not."""
    if regex:
        return cast(
            SectionScan,
            run_bounded(
                {
                    "kind": "sections",
                    "pattern": matcher.pattern,
                    "flags": matcher.flags,
                    "db_path": _db_path_ro(RFC_CORPUS),
                    "names": scoped,
                    "keep": keep,
                    "context": context,
                    "check_every": _RFC_SCAN_CHECK_EVERY,
                    "per_file_cap": _RFC_SECTIONS_PER_DOC,
                    "matches_per_section": _RFC_MATCHES_PER_SECTION,
                },
                _RFC_SCAN_BUDGET_S,
                _SECTION_SCAN_KEYS,
            ),
        )
    return scan_sections(
        matcher,
        iter_sections(RFC_CORPUS, scoped),
        keep,
        context,
        time.monotonic() + _RFC_SCAN_BUDGET_S,
        _RFC_SCAN_CHECK_EVERY,
        _RFC_SECTIONS_PER_DOC,
        _RFC_MATCHES_PER_SECTION,
    )


def _incomplete_reply(pattern: str, mode: str, found_note: str, read_note: str) -> str:
    return (
        _incomplete_scan_head(pattern, mode, _RFC_SCAN_BUDGET_S, found_note, read_note)
        + f"\n\n_{provenance_line()}_"
    )


def _grep_rfc_corpus(  # noqa: PLR0911
    pattern: str,
    file_pattern: Optional[str],
    regex: bool,
    case_sensitive: bool,
    limit: int,
    context: int,
    files_only: bool,
) -> str:
    """Literal scan over the assembled section text of the RFC series."""
    try:
        matcher = _compile_phrase(pattern, regex, case_sensitive)
    except re.error as exc:
        return (
            f"Invalid regex `{pattern}`: {exc}. Fix the pattern, or drop "
            "`regex=True` to search for it as a literal string."
        )

    files, refusal = _rfc_scan_scope(file_pattern)
    if refusal is not None:
        return refusal

    # A glob is the only reason to name files: an unscoped scan would otherwise
    # build an `IN (?,?,…)` clause with one parameter per RFC — 9,813 of them,
    # past the 999-parameter default of SQLite before 3.32, which older
    # distribution Pythons still ship. That is exactly where a swallowed error
    # would have turned into a confident claim of absence.
    scoped = files if file_pattern else None
    keep = 0 if files_only else _MAX_GREP_LIMIT
    mode = _mode_phrase(regex, case_sensitive) + (
        "" if regex else ", across line breaks"
    )
    try:
        scan = _run_scan(matcher, regex, scoped, keep, context)
    except ScanTimedOut:
        _note_scan_stop("timeout")
        # The kill leaves no counts to report; same rule as a stopped scan.
        return _incomplete_reply(
            pattern, mode, "no result was recovered", "an unknown number of sections"
        )
    except ScanFailed as exc:
        _note_scan_stop("failed", exc)
        return _SCAN_FAILED_REPLY
    sections = scan["sections"]
    if scan["error"] is not None:
        # An incomplete scan must never render as absence. Whatever was found
        # before the failure is still true, but the denominator is not, so the
        # one thing this must not do is stay quiet.
        return (
            f"The scan of the RFC series failed after {sections} section(s): "
            f"{scan['error']}. **Nothing can be concluded from this** — in "
            "particular a partial scan is not evidence that the pattern is "
            "absent. Retry, or narrow with `file_pattern`."
        )
    per_doc: Dict[str, int] = {}
    for file, count in scan["per_file"].items():
        per_doc[_rfc_number(file)] = count
    total = scan["total"]
    matching_sections = scan["matching_sections"]
    hits = [
        _RfcHit(_rfc_number(file), section, title, found, excerpts_)
        for file, section, title, found, excerpts_ in scan["hits"]
    ]

    if scan["stopped_early"]:
        # Same rule as a failed scan: a truncated denominator cannot support a
        # negative, so say what was covered and refuse to imply the rest.
        found_note = (
            f"{total} match(es) in {len(per_doc)} RFC(s) before it stopped"
            if per_doc
            else "no matches before it stopped"
        )
        return _incomplete_reply(
            pattern, mode, found_note, f"{sections} of the corpus's section(s)"
        )
    if not per_doc:
        body = _rfc_no_match_body(pattern, file_pattern, mode, len(files), sections)
        return f"{body}\n\n_{provenance_line()}_"

    header = (
        f"_grep `{pattern}` in the RFC series ({mode}): {total} match(es) in "
        f"{len(per_doc)} RFC(s); "
        f"{_rfc_scope_phrase(file_pattern, len(files), sections)}._"
    )
    if files_only:
        body = _render_rfc_files_only(per_doc, total, header, limit)
    else:
        body = _render_rfc_hits(hits, matching_sections, header, limit)
    return f"{body}\n\n_{provenance_line()}_"
