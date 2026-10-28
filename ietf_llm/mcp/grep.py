"""Literal-search tool: grep_corpus.

The lexical counterpart to `search_corpus`. Semantic search answers "what was
said about X"; it is structurally unable to answer "was X *ever* said", because
non-retrieval by embedding similarity is weak evidence of absence. This scans
the gathered files themselves, line by line, and reports the denominator it
scanned — so a zero here is a positive statement about the corpus rather than a
failure to retrieve.

It also reaches material the index cannot: concluded-draft revisions are not
embedded, so wording that lived only in a superseded revision is invisible to
`search_corpus` but present on disk and findable here.

Read-only and offline like every other read tool, and needs no embedding index
— only the files. The index is consulted opportunistically, to attribute a
matching line to the chunk containing it.

**Two backends behind one tool.** A gathered corpus is scanned as files, a line
at a time. The RFC series (`corpus="rfcs"`) has no files to scan — only the
`embeddings.db` that `ietf-llm --init` installs — so it is scanned as assembled
section text instead, which also lets a phrase match across the 72-column wrap.
The dispatcher at `tool_grep_corpus` picks; the RFC scan lives in `grep_rfc`,
where the differences a caller sees are set out above `_grep_rfc_corpus`.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import TYPE_CHECKING, Dict, List, NamedTuple, Optional, Tuple

from .. import coverage
from ..embeddings import chunk_spans
from .common import (
    _append_participation_nudge,
    _files_dir,
    _offload,
    _requires_corpus,
    _with_freshness,
)
from .grep_common import _MAX_GREP_LIMIT, _elide, _mode_phrase
from .grep_rfc import _grep_rfc_corpus
from .rfc_text import RFC_CORPUS

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


#: Cap on `context` lines either side of a hit.
_MAX_CONTEXT = 5

#: Extensions skipped by the scan: binary payloads the corpus carries
#: alongside its text (a slide deck, an image). Decoding them with
#: `errors="replace"` would not crash, but it would burn time and could
#: produce meaningless "matches" out of mojibake.
#: The `.kinds.json` digest sidecar is machine data, not corpus text.
_SKIP_SUFFIXES = (
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".zip",
    ".gz",
    ".db",
    ".kinds.json",
)

#: Directories holding a second copy of material that already has a
#: first-class per-item file: `raw/` is the year-bundled mail dump behind
#: `threads/`, `github/` the issue-archive JSON behind `issues/`. Scanning them
#: by default would report every mailing-list hit twice and spend the render
#: budget on the duplicate. Excluded unless the caller's `file_pattern` names
#: one of them explicitly — which is what makes the exclusion safe: it is a
#: default, not a wall, and the scope line always says it applied.
_DERIVED_PREFIXES = ("raw/", "github/")


class _Hit(NamedTuple):
    """One matching line: where it is and what it says."""

    file: str
    lineno: int
    text: str


def _compile(pattern: str, regex: bool, case_sensitive: bool) -> "re.Pattern[str]":
    """Compile the caller's pattern. A literal pattern is escaped rather than
    handled by a separate substring path, so matching, counting, and context
    rendering all run through one code path.

    `re.MULTILINE` is not optional. `_scan_file` prefilters by searching the
    file's whole text before it looks at individual lines, so without it `^`
    and `$` would anchor to the file rather than the line: `^From:` would fail
    the prefilter, the file would be skipped entirely, and the tool would
    report a confident zero — the exact unsound negative it exists to prevent.
    It also makes regex mode agree with the documented "matches within one
    line" contract.
    """
    flags = re.MULTILINE if case_sensitive else re.MULTILINE | re.IGNORECASE
    return re.compile(pattern if regex else re.escape(pattern), flags)


def _scan_file(
    path: str,
    relpath: str,
    matcher: "re.Pattern[str]",
    hits: List[_Hit],
    keep: int,
    per_file_cap: Optional[int] = None,
) -> int:
    """Count every matching line in one file; append the first `keep` of them
    (across the whole scan) to `hits`, and no more than `per_file_cap` from
    this file. Returns the count, which is complete whether or not the lines
    were retained.

    Reads the file whole and prefilters with a single search over the entire
    text: the overwhelmingly common case is a file with no match at all, and
    one C-level scan is far cheaper than splitting a 30 KB message thread into
    lines to check each one.

    Counting past `keep` but not retaining is what lets the reported total stay
    honest without holding a `_Hit` per match: a one-character pattern matches
    over a million lines in a real corpus, and keeping them all would be a
    memory-exhaustion lever on a shared server. Files are scanned in relpath
    order, so the retained hits are exactly the ones the renderer would have
    shown after sorting.

    The per-file cap is applied here, before the total `keep` is spent, which
    is what stops a file with hundreds of hits from using up the whole budget
    ahead of the files sorted after it. With a cap, `hits` is therefore no
    longer a prefix of the sorted matches, but it is still in (file, line)
    order.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return 0
    if not matcher.search(text):
        return 0
    found = 0
    kept = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if matcher.search(line):
            found += 1
            if len(hits) < keep and (per_file_cap is None or kept < per_file_cap):
                hits.append(_Hit(relpath, lineno, line))
                kept += 1
    return found


def _chunk_for_line(
    spans: List[Tuple[int, int, int, str]], lineno: int
) -> Optional[Tuple[int, str]]:
    """The (chunk_idx, title) whose line span contains `lineno`, or None.

    Linear scan: a file's span list is short (one entry per message / window)
    and only files that actually matched are looked up at all.
    """
    for chunk_idx, start, end, title in spans:
        if start <= lineno <= end:
            return (chunk_idx, title)
    return None


def _context_lines(
    path: str, lineno: int, context: int, cache: Dict[str, List[str]]
) -> Tuple[List[str], List[str]]:
    """The `context` lines before and after `lineno` in `path`.

    `cache` memoises the file's lines across the hits being rendered — a
    matching file usually carries several hits, and re-reading it once per hit
    is the one place this tool would do real needless I/O.
    """
    lines = cache.get(path)
    if lines is None:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        cache[path] = lines
    before = lines[max(0, lineno - 1 - context) : lineno - 1]
    after = lines[lineno : lineno + context]
    return ([_elide(line) for line in before], [_elide(line) for line in after])


def _wants_derived(file_pattern: Optional[str]) -> bool:
    """True when the caller's glob explicitly reaches into a derived-duplicate
    directory, which opts that directory back into the scan."""
    if not file_pattern:
        return False
    return file_pattern.lower().startswith(_DERIVED_PREFIXES)


def _scope_phrase(
    file_pattern: Optional[str], scanned: int, skipped: int, derived: int
) -> str:
    """How much of the corpus this scan actually covered — the denominator a
    negative result has to be read against."""
    scope = f"`{file_pattern}`" if file_pattern else "the whole gathered cache"
    out = f"scanned {scanned} file(s) — {scope}"
    if skipped:
        out += f"; {skipped} binary file(s) skipped"
    if derived:
        out += (
            f"; {derived} duplicate file(s) under `raw/` / `github/` skipped "
            "(same content as `threads/` / `issues/`)"
        )
    return out


def _eligible(
    cache: str, file_pattern: Optional[str]
) -> Tuple[List[Tuple[str, str]], int, int]:
    """The files this call will scan, as `(relpath, path)` sorted by relpath,
    plus the counts excluded as binary and as derived duplicates.

    Collected up front, and sorted, for two reasons: the scan can then stop
    retaining hits at the render cap and still hold exactly the ones the
    renderer would have shown, and the output is deterministic rather than
    dependent on directory-walk order.
    """
    keep_derived = _wants_derived(file_pattern)
    eligible: List[Tuple[str, str]] = []
    skipped = 0
    derived = 0
    for dirpath, _dirnames, filenames in os.walk(cache):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            relpath = os.path.relpath(path, cache)
            if file_pattern is not None and not fnmatch.fnmatch(relpath, file_pattern):
                continue
            if name.lower().endswith(_SKIP_SUFFIXES):
                skipped += 1
                continue
            if not keep_derived and relpath.lower().startswith(_DERIVED_PREFIXES):
                derived += 1
                continue
            eligible.append((relpath, path))
    eligible.sort(key=lambda entry: entry[0])
    return eligible, skipped, derived


def _nothing_scanned_body(
    wg: str, file_pattern: Optional[str], skipped: int, derived: int
) -> str:
    """Explain a scan with an empty denominator.

    Distinct from `_no_match_body` on purpose: "I read 3448 files and the
    string is not in them" and "I read nothing" are different findings, and
    only the first supports a claim of absence. Reaching here means the glob
    matched no file, or matched only files the scan excludes.
    """
    where = f"`{file_pattern}`" if file_pattern else "the gathered cache"
    reasons: List[str] = []
    if skipped:
        reasons.append(f"{skipped} binary file(s)")
    if derived:
        reasons.append(f"{derived} duplicate file(s) under `raw/` / `github/`")
    if reasons:
        lines = [
            f"(nothing was scanned: everything matching {where} was excluded "
            f"— {', '.join(reasons)}.)",
            "",
            "_This is **not** evidence of absence — no file was read. "
            + ("Binary files (slides, images) are never scanned. " if skipped else "")
            + (
                "The `raw/` and `github/` copies are skipped because they "
                "duplicate `threads/` and `issues/`; pass a `file_pattern` "
                "starting `raw/` or `github/` to scan them directly."
                if derived
                else ""
            )
            + "_",
        ]
        return "\n".join(lines)
    return (
        f"(no files match `{file_pattern}`, so nothing was scanned — this is "
        "**not** evidence of absence. Try a broader glob, e.g. `threads/*` or "
        f"`drafts/*`, or check `list_files('{wg}')`.)"
        if file_pattern
        else f"(no readable files in the {wg} cache, so nothing was scanned — "
        "this is **not** evidence of absence. The corpus may have gathered no "
        "content; check `list_files` and the gather notes.)"
    )


def _github_edge(cache: str, eligible: List[Tuple[str, str]]) -> str:
    """The GitHub record's ceiling, as a bound on this zero — empty unless the
    scan covered `issues/` / `pulls/`, since on a mail-only scope it would
    limit something the caller didn't ask about. Parses the archives, which is
    why only the zero path calls it: that is the one place this tool invites a
    positive claim of absence, and so the one place the parse earns itself."""
    if not any(relpath.startswith(("issues/", "pulls/")) for relpath, _ in eligible):
        return ""
    return coverage.record_edge_line(coverage.github_records(cache))


def _no_match_body(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    wg: str,
    pattern: str,
    file_pattern: Optional[str],
    mode: str,
    scanned: int,
    skipped: int,
    derived: int,
    edge: str = "",
) -> str:
    """Render a zero-result scan as the finding it is.

    A caller reaches for this tool precisely when it needs to say "this was
    never said". So the empty case has to state its own scope and its own
    failure modes, or the caller will either overclaim (absence in the
    gathered window read as absence in the record) or discard a sound result.
    """
    lines = [
        f"(no matches for `{pattern}` in {wg} — {mode}; "
        f"{_scope_phrase(file_pattern, scanned, skipped, derived)}.)",
        "",
        "_This is a complete line-by-line scan of the files named above, so it is "
        "real evidence of absence **within them** — unlike a semantic miss. "
        "Three limits still bound the claim: the gather window (the freshness "
        "and coverage lines above say how far back and how recently this "
        "corpus reaches), the corpus boundary (another list or repo may hold "
        "it — try `search_corpora` / `find_efforts`), and line breaks: this "
        "matches within a single line, so a phrase split across a mail wrap "
        "will be missed. Search the most distinctive single token (`8890`, "
        "not `RFC 8890`) before concluding anything._",
    ]
    if edge:
        lines.append(
            f"\n_The GitHub record scanned here ends at {edge}. Issues and PRs "
            "above that number were never gathered, so this zero is silent "
            "about them — re-gather before reading it as absence._"
        )
    if file_pattern:
        lines.append(
            f"\n_Scoped to `{file_pattern}` — re-run without `file_pattern` "
            "before treating this as absence from the corpus._"
        )
    return "\n".join(lines)


def _next_footer(wg: str) -> str:
    """The pivot hint. Emitted by both renderers — a `files_only` caller has
    just been handed a file list and is the *most* likely to want the next
    hop, so omitting it there would be backwards."""
    return (
        f"\n_Next: read a hit in context with "
        f'`read_file_section("{wg}", "<file>", start_line=<line>)`, or '
        f'`get_chunk_text("{wg}", "<file>", <chunk>)` for the whole message '
        "where a chunk is named. `search_corpus` for the semantic view._"
    )


def _render_files_only(
    wg: str, per_file: Dict[str, int], total: int, header: str, limit: int
) -> str:
    """One row per file with its match count — the breadth view, for "which
    threads ever mention X" rather than "show me every line". Here `limit`
    bounds rows, i.e. files, since no lines are rendered."""
    ranked = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ranked[:limit]
    out = [header, ""]
    for relpath, count in shown:
        out.append(f"{count:>6} match(es)  {relpath}")
    if len(ranked) > len(shown):
        out.append(
            f"\n_{len(ranked) - len(shown)} more file(s) matched "
            f"(showing {len(shown)} of {len(ranked)}); raise `limit`._"
        )
    out.append(
        f"\n_{total} matching line(s) across {len(ranked)} file(s). "
        "Drop `files_only` to see the lines themselves._"
    )
    out.append(_next_footer(wg))
    return "\n".join(out)


def _hits_footer(
    hits: List[_Hit],
    per_file: Dict[str, int],
    total: int,
    max_per_file: Optional[int],
) -> str:
    """The note under a capped listing, or "" when nothing was cut.

    Lines can be cut by two caps that compose in a fixed order: `max_per_file`
    first, then `limit` over what is left. So the shortfall splits cleanly
    into the part the per-file cap removed and the part `limit` removed, and
    the note names each, so a caller knows which knob to turn.
    """
    if max_per_file is None:
        if total > len(hits):
            return (
                f"\n_Showing {len(hits)} of {total} matching line(s) — the "
                "count is the complete scan, only the listing is capped. "
                f"Raise `limit` (max {_MAX_GREP_LIMIT}), narrow with "
                "`file_pattern`, or pass `files_only=True` for one row per "
                "file._"
            )
        return ""
    after_cap = sum(min(count, max_per_file) for count in per_file.values())
    by_cap = total - after_cap
    by_limit = after_cap - len(hits)
    if not by_cap and not by_limit:
        return ""
    reasons: List[str] = []
    if by_cap:
        capped = sum(1 for count in per_file.values() if count > max_per_file)
        reasons.append(
            f"{by_cap} omitted by `max_per_file={max_per_file}` "
            f"({capped} file(s) had more)"
        )
    if by_limit:
        reasons.append(f"{by_limit} omitted by `limit`")
    unlisted = len(per_file) - len({hit.file for hit in hits})
    if unlisted:
        reasons.append(f"{unlisted} matching file(s) have no line listed")
    # Advise only the cap that removed lines: raising one that cut nothing
    # would change nothing, and raising `max_per_file` while `limit` is the
    # binding cap would just spend the budget on fewer files.
    raise_what: List[str] = []
    if by_cap:
        raise_what.append("`max_per_file`")
    if by_limit:
        raise_what.append(f"`limit` (max {_MAX_GREP_LIMIT})")
    return (
        f"\n_Showing {len(hits)} of {total} matching line(s) — the count is "
        f"the complete scan, only the listing is capped: {'; '.join(reasons)}. "
        f"Raise {' or '.join(raise_what)}, narrow with `file_pattern`, or "
        "pass `files_only=True` for one row per file._"
    )


def _render_hits(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    wg: str,
    hits: List[_Hit],
    per_file: Dict[str, int],
    total: int,
    header: str,
    context: int,
    cache: str,
    matcher: "re.Pattern[str]",
    max_per_file: Optional[int] = None,
) -> str:
    """Render matching lines grouped by file, annotated with the chunk that
    contains each one where the index knows it.

    Under `max_per_file`, each file's group ends with its true match count, so
    a capped file reads as "3 of 120" rather than as a file with 3 matches.
    `context` lines are rendered around a shown hit but are never counted
    against either cap: both caps count matching lines.
    """
    spans_by_file = chunk_spans(wg, [hit.file for hit in hits])
    line_cache: Dict[str, List[str]] = {}
    out = [header, ""]
    current: Optional[str] = None
    shown_here = 0

    def _close_group() -> None:
        if max_per_file is not None and current and shown_here < per_file[current]:
            out.append(
                f"  … {shown_here} of {per_file[current]} matching line(s) "
                "in this file shown"
            )

    for hit in hits:
        if hit.file != current:
            _close_group()
            current = hit.file
            shown_here = 0
            out.append(f"## {hit.file}")
        shown_here += 1
        spans = spans_by_file.get(hit.file, [])
        where = _chunk_for_line(spans, hit.lineno)
        tag = f"  [chunk {where[0]}: {where[1]}]" if where else ""
        if context:
            before, after = _context_lines(
                os.path.join(cache, hit.file), hit.lineno, context, line_cache
            )
            for offset, line in enumerate(before, start=hit.lineno - len(before)):
                out.append(f"  {offset:>6}- {line}")
            out.append(f"  {hit.lineno:>6}: {_elide(hit.text, matcher)}{tag}")
            for offset, line in enumerate(after, start=hit.lineno + 1):
                out.append(f"  {offset:>6}- {line}")
            out.append("")
        else:
            out.append(f"  {hit.lineno:>6}: {_elide(hit.text, matcher)}{tag}")
    _close_group()
    footer = _hits_footer(hits, per_file, total, max_per_file)
    if footer:
        out.append(footer)
    out.append(_next_footer(wg))
    return "\n".join(out)


def tool_grep_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    wg: str,
    pattern: str,
    file_pattern: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
    context: int = 0,
    files_only: bool = False,
    max_per_file: Optional[int] = None,
) -> str:
    """Dispatch to the gathered-files scan or the RFC-series scan.

    The two share a tool because they answer the same question — "does this
    string appear, and where" — and a caller should not have to know that one
    reads files and the other reads a chunk table. They differ in what a hit
    can be located by; see the RFC section above.

    `max_per_file` is refused here rather than clamped: unlike `limit`, a zero
    or negative value has no sensible reading, and silently substituting one
    would present a different listing from the one asked for. It shapes only
    the gathered line view; the RFC scan already caps sections per RFC.
    """
    if not (pattern or "").strip():
        return (
            "grep_corpus needs a non-empty `pattern` — the literal string to "
            'look for, e.g. `grep_corpus("httpbis", "8890")`.'
        )
    if max_per_file is not None:
        # `True` would pass `int()` as 1, and `inf` raises OverflowError; neither
        # is a line count the caller meant.
        try:
            max_per_file = 0 if isinstance(max_per_file, bool) else int(max_per_file)
        except (TypeError, ValueError, OverflowError):
            max_per_file = 0
        if max_per_file < 1:
            return (
                "grep_corpus needs `max_per_file` to be a positive integer "
                "(lines to show per file), or unset for no per-file cap."
            )
    try:
        limit = max(1, min(int(limit), _MAX_GREP_LIMIT))
    except (TypeError, ValueError):
        limit = 50
    try:
        context = max(0, min(int(context), _MAX_CONTEXT))
    except (TypeError, ValueError):
        context = 0
    if wg == RFC_CORPUS:
        return _grep_rfc_corpus(
            pattern, file_pattern, regex, case_sensitive, limit, context, files_only
        )
    return _grep_gathered(
        wg,
        pattern,
        file_pattern,
        regex,
        case_sensitive,
        limit,
        context,
        files_only,
        max_per_file,
    )


@_requires_corpus
def _grep_gathered(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    wg: str,
    pattern: str,
    file_pattern: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 50,
    context: int = 0,
    files_only: bool = False,
    max_per_file: Optional[int] = None,
) -> str:
    # `pattern`, `limit`, `context` and `max_per_file` are already validated by
    # the dispatcher, which has to do it for both backends.
    try:
        matcher = _compile(pattern, regex, case_sensitive)
    except re.error as exc:
        return (
            f"Invalid regex `{pattern}`: {exc}. Fix the pattern, or drop "
            "`regex=True` to search for it as a literal string."
        )

    cache = _files_dir(wg)
    if not os.path.isdir(cache):
        return f"No cache for {wg}."

    eligible, skipped, derived = _eligible(cache, file_pattern)
    # `files_only` never renders a line, so it retains none: the per-file
    # counts are all it needs, and a broad pattern over a large corpus would
    # otherwise hold a hit per match for nothing.
    keep = 0 if files_only else limit
    hits: List[_Hit] = []
    per_file: Dict[str, int] = {}
    total = 0
    for relpath, path in eligible:
        found = _scan_file(path, relpath, matcher, hits, keep, max_per_file)
        if found:
            per_file[relpath] = found
            total += found
    scanned = len(eligible)

    mode = _mode_phrase(regex, case_sensitive)
    # Nothing scanned means there is no denominator, so the evidence-of-absence
    # body below would be a claim resting on zero files. Say what happened
    # instead — the glob matched nothing, or matched only files the scan
    # excludes — and how to reach them.
    if scanned == 0:
        return _with_freshness(
            wg, _nothing_scanned_body(wg, file_pattern, skipped, derived)
        )
    if not hits and not per_file:
        edge = _github_edge(cache, eligible)
        body = _no_match_body(
            wg, pattern, file_pattern, mode, scanned, skipped, derived, edge
        )
        return _with_freshness(wg, body)

    # `eligible` is relpath-sorted and retention stopped at `keep`, so `hits`
    # is already in (file, line) order — no sort needed. Without a per-file cap
    # it is the first `limit` matches; with one, the first `max_per_file` of
    # each file, up to `limit` in all.
    header = (
        f"_grep `{pattern}` in {wg} ({mode}): {total} matching line(s) in "
        f"{len(per_file)} file(s); "
        f"{_scope_phrase(file_pattern, scanned, skipped, derived)}._"
    )
    if files_only:
        body = _render_files_only(wg, per_file, total, header, limit)
    else:
        body = _render_hits(
            wg, hits, per_file, total, header, context, cache, matcher, max_per_file
        )
    return _append_participation_nudge(list(per_file.keys()), _with_freshness(wg, body))


# --- MCP server wiring -------------------------------------------------------


def register(server: "FastMCP") -> None:
    @server.tool()
    async def grep_corpus(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        corpus: str,
        pattern: str,
        file_pattern: Optional[str] = None,
        regex: bool = False,
        case_sensitive: bool = False,
        limit: int = 50,
        context: int = 0,
        files_only: bool = False,
        max_per_file: Optional[int] = None,
    ) -> str:
        """Exact-string (or regex) search over a corpus's gathered files —
        the lexical counterpart to `search_corpus`. Returns matching lines as
        `file` + line number, each annotated with the chunk that contains it
        where the index knows, plus the count of files scanned.

        **Use this whenever the exact token IS the question**, and especially
        for any *negative* claim. `search_corpus` ranks by embedding
        similarity, so a miss there is weak evidence of absence — a query for
        "has anyone cited RFC 8890" can return unrelated threads and prove
        nothing. This scans every file and reports its denominator, so a zero
        is a finding you can state. Reach for it on:

        - "has X ever been said / cited / proposed here?"
        - "does this corpus mention `fail closed` / `X.509` anywhere?"
        - "which revision first used this parameter name?"

        Stay with `search_corpus` for "what was said about X" and "what is the
        argument for Y" — meaning, not spelling, is what that indexes.

        It also sees what the index cannot: concluded-draft revisions are not
        embedded, so wording that appeared only in a superseded revision is
        findable here and nowhere else.

        **`corpus="rfcs"` searches the full text of the whole RFC series**,
        and is the way to answer "which RFCs contain this exact sentence" —
        the question `search_rfc_text` cannot answer, because that ranks by
        embedding similarity and never matches a string. It differs from a
        gathered corpus in two ways:

        - A hit is located as **RFC + section**, not file + line — the RFC
          index carries no line numbers. Pivot with `get_rfc_section`.
        - A literal pattern **matches across line breaks**, since RFC text is
          hard-wrapped at 72 columns. So a whole sentence works here, where in
          a gathered corpus it would be split by the wrap and missed.

        Front and back matter (status of this memo, copyright, authors'
        addresses, the reference list) is not indexed, so it bounds a negative
        result — the zero-match reply says so. `file_pattern` globs the corpus
        filename, which is `rfc9111.txt`.

        **Everywhere else, a match is within one line.** A phrase broken
        across a mail wrap will not match. Search the most distinctive single
        token — `8890`, not `RFC 8890` — then widen once you have the files.

        A zero over `issues/` / `pulls/` also reports **where the GitHub
        record stops** — the highest issue/PR number held, and when that
        archive was built. An issue filed above that number was never scanned,
        so check the ceiling before reading a zero as "never raised".

        Options:
          - `file_pattern`: glob over the relative path (`threads/*`,
            `drafts/draft-ietf-httpbis-*`, `*mlkem*`), the same glob
            `list_files` takes. Narrowing also narrows what a zero result can
            claim, so scan unscoped before concluding absence.
          - `regex=True`: treat `pattern` as a Python regex instead of a
            literal string. An invalid pattern is reported, not raised.
          - `case_sensitive=True`: default is case-insensitive.
          - `limit`: cap on rendered rows — matching lines normally, files
            under `files_only` (default 50, max 200). The scan and the
            reported totals are always complete; only the listing is cut.
          - `max_per_file=N`: show at most N matching lines per file, so
            one noisy file cannot use up `limit` ahead of the others. Applied
            before `limit`, which still bounds the total lines, so a small N
            reaches more files but not necessarily all of them: the footer
            says how many matching files got no line, and how many lines each
            cap omitted. Counts matching lines only; `context` lines are
            extra. Totals and per-file counts stay complete.
            Unset (default) means no per-file cap; must be at least 1.
            Gathered corpora only: ignored under `files_only`, and the RFC
            series already shows at most 3 sections per RFC.
          - `context=N`: N lines either side of each hit (max 5).
          - `files_only=True`: one row per file with its match count — the
            breadth view for "which threads mention X at all".

        `raw/` (the year-bundled mail dump) and `github/` (the issue-archive
        JSON) hold a second copy of what `threads/` and `issues/` already
        carry, so they are skipped by default — otherwise every list hit
        reports twice. A `file_pattern` starting `raw/` or `github/` scans
        them anyway, and the scope line always says when the skip applied.

        Read-only, offline, and needs no embedding index — only the gathered
        files. Pivot to `read_file_section` or `get_chunk_text` to read a hit
        in context.
        """
        return await _offload(
            tool_grep_corpus,
            corpus,
            pattern,
            file_pattern=file_pattern,
            regex=regex,
            case_sensitive=case_sensitive,
            limit=limit,
            context=context,
            files_only=files_only,
            max_per_file=max_per_file,
        )
