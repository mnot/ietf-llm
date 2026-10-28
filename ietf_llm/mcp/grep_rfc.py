"""The RFC-series backend of `grep_corpus` (`corpus="rfcs"`).

`grep.tool_grep_corpus` dispatches here; the differences a caller sees from
the gathered-corpus scan are set out above `_grep_rfc_corpus`.
"""

from __future__ import annotations

import bisect
import fnmatch
import re
import sqlite3
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

from ..embeddings.storage import indexed_files, iter_sections
from .grep_common import _MAX_GREP_LIMIT, _elide, _mode_phrase
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
#: either is adversarial. Every call re-scans from scratch on an `_offload`
#: worker, so without a bound a few concurrent calls saturate the pool on the
#: shared deployment. Stopping and *saying so* is the only safe behaviour: a
#: truncated scan that rendered as a normal result would be the unsound
#: negative this tool exists to prevent.
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


def _excerpts(
    text: str, matcher: "re.Pattern[str]", context: int, max_matches: int
) -> List[List[str]]:
    """The lines covering each of the first `max_matches` matches in `text`,
    with `context` lines either side.

    A match may straddle a wrap, so the span is taken from the line holding its
    first character to the line holding its last — rendering only the opening
    line would cut the phrase the caller searched for in half.
    """
    lines = text.splitlines()
    offsets: List[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1
    out: List[List[str]] = []
    for count, match in enumerate(matcher.finditer(text)):
        if count >= max_matches:
            break
        first = max(0, bisect.bisect_right(offsets, match.start()) - 1)
        last = max(
            first, bisect.bisect_right(offsets, max(match.start(), match.end() - 1)) - 1
        )
        low = max(0, first - context)
        high = min(len(lines), last + 1 + context)
        out.append([_elide(line) for line in lines[low:high]])
    return out


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


def _grep_rfc_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
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

    hits: List[_RfcHit] = []
    per_doc: Dict[str, int] = {}
    total = 0
    sections = 0
    matching_sections = 0
    stopped_early = False
    keep = 0 if files_only else _MAX_GREP_LIMIT
    deadline = time.monotonic() + _RFC_SCAN_BUDGET_S
    # A glob is the only reason to name files: an unscoped scan would otherwise
    # build an `IN (?,?,…)` clause with one parameter per RFC — 9,813 of them,
    # past the 999-parameter default of SQLite before 3.32, which older
    # distribution Pythons still ship. That is exactly where a swallowed error
    # would have turned into a confident claim of absence.
    scoped = files if file_pattern else None
    try:
        stream = iter_sections(RFC_CORPUS, scoped)
        for file, section, title, text in stream:
            sections += 1
            if not sections % _RFC_SCAN_CHECK_EVERY and time.monotonic() > deadline:
                stopped_early = True
                stream.close()
                break
            found = sum(1 for _ in matcher.finditer(text))
            if not found:
                continue
            matching_sections += 1
            number = _rfc_number(file)
            total += found
            per_doc[number] = per_doc.get(number, 0) + found
            if len(hits) >= keep or sum(1 for h in hits if h.number == number) >= (
                _RFC_SECTIONS_PER_DOC
            ):
                continue
            hits.append(
                _RfcHit(
                    number,
                    section,
                    title,
                    found,
                    _excerpts(text, matcher, context, _RFC_MATCHES_PER_SECTION),
                )
            )
    except sqlite3.Error as exc:
        # An incomplete scan must never render as absence. Whatever was found
        # before the failure is still true, but the denominator is not, so the
        # one thing this must not do is stay quiet.
        return (
            f"The scan of the RFC series failed after {sections} section(s): "
            f"{exc}. **Nothing can be concluded from this** — in particular a "
            "partial scan is not evidence that the pattern is absent. Retry, "
            "or narrow with `file_pattern`."
        )

    mode = _mode_phrase(regex, case_sensitive) + (
        "" if regex else ", across line breaks"
    )
    if stopped_early:
        # Same rule as a failed scan: a truncated denominator cannot support a
        # negative, so say what was covered and refuse to imply the rest.
        found_note = (
            f"{total} match(es) in {len(per_doc)} RFC(s) before it stopped"
            if per_doc
            else "no matches before it stopped"
        )
        return (
            f"**INCOMPLETE SCAN — stopped after {_RFC_SCAN_BUDGET_S:.0f}s.** "
            f"`{pattern}` ({mode}): {found_note}, having read {sections} of "
            f"the corpus's section(s). A regex over ~400 MB can run for "
            "minutes; a literal phrase scans the whole series in seconds. "
            "**This cannot support a negative claim** — narrow with "
            "`file_pattern`, or search a literal phrase instead.\n\n"
            f"_{provenance_line()}_"
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
