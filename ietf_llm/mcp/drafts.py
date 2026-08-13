"""Draft / issue tools: draft_authors, list_drafts, get_draft,
get_issue, draft_status, review_record."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ..freshness import gather_suggestion
from ..gather.sources.citations import normalize_draft_name
from ..gather.sources.documents_manifest import load_documents_manifest
from ..paths import drafts_dir, issue_path, issues_dir, pull_path, pulls_dir
from .common import _files_dir, _list_wgs, _offload, _requires_corpus, _with_freshness
from .rfc_text import normalise_section

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover

    from ..live_lookup.reviews import ReviewRecord  # pragma: no cover


def _find_latest_draft_file(name: str) -> Optional[str]:
    """Locate the highest-revision cached `.txt` for a draft across all corpora.

    A draft name (e.g. `draft-ietf-httpbis-foo`) implies its WG but can be
    cached under more than one corpus; this scans every gathered corpus's
    `drafts/` dir and returns the newest revision found anywhere, or None.
    Read-only — uses the same per-corpus files dir the read tools resolve.
    """
    base = normalize_draft_name(name)
    pattern = re.compile(rf"^{re.escape(base)}-(\d+)\.txt$")
    best_version = -1
    best_path: Optional[str] = None
    for wg in _list_wgs():
        try:
            cache = _files_dir(wg)
        except FileNotFoundError:
            continue
        ddir = drafts_dir(cache)
        if not os.path.isdir(ddir):
            continue
        for fname in os.listdir(ddir):
            match = pattern.match(fname.lower())
            if not match:
                continue
            version = int(match.group(1))
            if version > best_version:
                best_version = version
                best_path = os.path.join(ddir, fname)
    return best_path


def tool_draft_authors(name: str) -> str:
    """Render a draft's authors/editors with contact emails, from the cache.

    Reads the Authors' Addresses section of the newest cached revision (the
    same text gather already parses) — no network. Returns what the draft
    itself records; a chair may know a better working address from mail and
    can override.
    """
    from ..gather.sources.draft_authors import (  # pylint: disable=import-outside-toplevel
        parse_authors,
    )

    name = (name or "").strip()
    if not name:
        return (
            "Provide a draft name, e.g. `draft-ietf-httpbis-resumable-upload` "
            "(the version suffix is optional)."
        )
    base = normalize_draft_name(name)
    path = _find_latest_draft_file(name)
    if path is None:
        return (
            f"No cached copy of `{base}` in any gathered corpus. Gather the "
            "owning corpus (its WG) first, then retry — author contacts are "
            "read from the cached draft text."
        )
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return f"Could not read the cached draft `{os.path.basename(path)}`."

    authors = parse_authors(text)
    fname = os.path.basename(path)
    if not authors:
        return (
            f"No parseable Authors' Addresses section in the cached `{fname}` — "
            "read the draft's tail directly with `read_file_section`."
        )
    lines = [
        f"# Authors of {base}\n",
        f"_From the Authors' Addresses section of the cached `{fname}`. "
        "These are the draft-stated addresses; a chair may have a better "
        "working address from mail._\n",
    ]
    for author in authors:
        role = "editor" if author.is_editor else "author"
        org = f", {author.organization}" if author.organization else ""
        email = author.email or "_no email listed_"
        lines.append(f"- **{author.name}** ({role}){org} — {email}")
    return "\n".join(lines)


#: Coarse draft lifecycle slugs (Datatracker draft-type states) → friendly
#: labels. This is the whole offline vocabulary — WG-process granularity (WGLC,
#: IESG evaluation) is not persisted and lives only in the live `draft_status`.
_DRAFT_LIFECYCLE_LABELS = {
    "active": "active I-D",
    "expired": "expired",
    "rfc": "published RFC",
    "repl": "replaced",
    "auth-rm": "withdrawn (author)",
    "ietf-rm": "withdrawn (IETF)",
}


@_requires_corpus
def tool_list_drafts(wg: str, state: str = "") -> str:
    manifest = load_documents_manifest(wg)
    if not manifest:
        return _with_freshness(
            wg,
            f"No draft lifecycle state recorded for {wg} — "
            f"{gather_suggestion(wg, purpose='to record it')}.",
        )
    rows = []
    for name in sorted(manifest):
        slug = manifest[name].get("state") or "unknown"
        if state and slug != state:
            continue
        expires = manifest[name].get("expires") or ""
        rows.append((name, _DRAFT_LIFECYCLE_LABELS.get(slug, slug), expires))
    if not rows:
        present = ", ".join(
            sorted({(rec.get("state") or "unknown") for rec in manifest.values()})
        )
        return _with_freshness(
            wg, f"No drafts in state '{state}' for {wg}. States present: {present}."
        )
    name_w = max(len(n) for n, _, _ in rows)
    label_w = max(len(lab) for _, lab, _ in rows)
    lines = [
        f"{n.ljust(name_w)}  {lab.ljust(label_w)}  {e}".rstrip() for n, lab, e in rows
    ]
    body = (
        f"Draft lifecycle state for {wg} (name · state · expires), offline from "
        "the cache. This is the COARSE lifecycle only — active / expired / "
        "became-RFC / replaced / withdrawn. It does NOT include WG-process state "
        "(WG Last Call, IESG evaluation); for that use the live `draft_status`. "
        "Adoption is derivable from the name (`draft-ietf-<wg>-` is adopted).\n\n"
        + "\n".join(lines)
    )
    return _with_freshness(wg, body)


#: Line caps for the verbatim artifact reads, so one call can't blow the
#: context window; both page via their start_line / read_file_section hint.
_DRAFT_MAX_LINES = 2000


_ISSUE_MAX_LINES = 3000


#: The bound that actually binds. A line cap is the wrong unit: 2000 lines of
#: draft text is ~88,000 characters, which overruns an MCP client's per-result
#: token limit, so the client truncates *again* — and its cut lands wherever it
#: lands, silently, after our own "continue with start_line=N" footer has been
#: chopped off the end. Two truncations disagreeing is how a caller ends up
#: reading half a document believing they read all of it. Bounding by
#: characters keeps a result under any plausible client limit, which is what
#: makes our own truncation notice the only one and therefore the true one.
_MAX_WINDOW_CHARS = 40_000


def _truncation_notice(start: int, end: int, total: int, extra: str = "") -> str:
    """The banner for a partial read.

    Deliberately not in the document's voice: the failure this exists to stop
    was a `#`-prefixed header line reading as part of the draft it introduced.
    It is emitted at both ends, because a client that truncates further eats
    the tail first.
    """
    shown = end - start + 1
    return (
        f"**PARTIAL READ — lines {start}–{end} of {total} "
        f"({shown * 100 // max(1, total)}% of the file).** "
        f"Continue with `start_line={end + 1}`{extra}."
    )


def _read_file_window(
    path: str,
    start_line: int,
    max_lines: int,
    complete_at: Optional[int] = None,
    section_hint: str = "",
) -> str:
    """Return a bounded, header-stamped line window of `path`, banner-stamped
    at both ends when it is partial.

    Bounded by `max_lines` *and* `_MAX_WINDOW_CHARS`, whichever binds first;
    the banner reports the cut that actually happened, so it never promises
    lines the caller did not get.

    `complete_at` is the line at which the caller's request is satisfied — the
    end of the file for a page read, the end of the section for a section
    read. Without it a fully-delivered section would be stamped "partial"
    merely because the *document* continues, which is the same
    read-it-as-content confusion this banner exists to end.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return f"Could not read `{os.path.basename(path)}`."
    total = len(lines)
    start = max(1, start_line)
    if start > total:
        return (
            f"# {os.path.basename(path)} ({total} lines)\n\n"
            f"_(start_line={start_line} is past the end — the file has "
            f"{total} lines.)_\n"
        )
    satisfied = min(total, complete_at if complete_at is not None else total)
    end = min(total, start - 1 + max(1, max_lines))
    body: List[str] = []
    budget = _MAX_WINDOW_CHARS
    for offset, line in enumerate(lines[start - 1 : end]):
        if budget - len(line) < 0:
            if body:
                end = start + offset - 1
                break
            # One line longer than the whole budget: a pasted base64 blob or
            # log dump, which `get_issue` does see. Emitting it whole to avoid
            # an empty result would put the character bound back where it
            # started, so it is cut instead — and saying so matters, because a
            # silently shortened line is a misquote.
            body.append(line[:budget].rstrip("\n") + " …[line truncated]\n")
            end = start + offset
            break
        body.append(line)
        budget -= len(line)
    header = f"# {os.path.basename(path)} (lines {start}–{end} of {total})\n\n"
    if end >= satisfied:
        return header + "".join(body)
    notice = _truncation_notice(start, end, total, section_hint)
    return f"{notice}\n\n{header}" + "".join(body) + f"\n{notice}\n"


#: A section heading at column 0: `1.  Introduction`, `3.1.  Overview`,
#: `Appendix B.  Rationale`, `B.5.  ...`.
#:
#: A bare single letter must carry the `Appendix` / `Annex` word. Without that
#: the branch matches the initial in a column-0 running footer — `R. Perlman,
#: et. al.  Expires: 17 May 2001` becomes "Appendix R". `B.1` is unambiguous
#: and stands alone.
_DRAFT_HEADING_RE = re.compile(
    r"^(?:(?P<word>Appendix|Annex)\s+)?"
    r"(?P<label>[0-9]+(?:\.[0-9]+)*|[A-Z]\.[0-9]+(?:\.[0-9]+)*|[A-Z])\."
    r"\s+(?P<title>\S.*?)\s*$"
)

#: A run of dots is a table-of-contents leader, never a section title.
_TOC_LEADER_RE = re.compile(r"\.{4,}")


def _draft_headings(lines: List[str]) -> List[Tuple[str, str, int]]:
    """`(label, title, line_number)` for each section heading, in document
    order. Reader-side: parsed from the cached text on every call, so this
    needs no re-gather and works on every corpus already on disk.

    Column 0 alone is not enough to identify a heading, which an earlier
    version assumed. Two things also live at column 0 and match the shape:

    * a **table of contents** in the older non-xml2rfc layout, where entries
      are unindented (`1.  Introduction......4`); and
    * **numbered pseudocode**, common in CFRG drafts (`1.  L =
      length(messages)`), sometimes blank-line separated like a heading.

    Either produces duplicate labels, and a duplicate is how `section="1"`
    comes to return the table of contents instead of the introduction — a
    wrong answer delivered silently, which is the failure this tool exists to
    end. So a heading must also be surrounded by blank lines and carry no dot
    leader. Across the 2,389 cached drafts that takes drafts with duplicated
    labels from 38 to 10; the cost is 9 of 2,247 drafts with a table of
    contents losing an entry from their outline, which is recoverable — a
    missing row answers "no section X" and shows what is there.
    """
    out: List[Tuple[str, str, int]] = []
    for index, line in enumerate(lines):
        match = _DRAFT_HEADING_RE.match(line.rstrip("\n"))
        if match is None:
            continue
        label = match.group("label")
        if len(label) == 1 and label.isalpha() and not match.group("word"):
            continue
        if _TOC_LEADER_RE.search(match.group("title")):
            continue
        if index and lines[index - 1].strip():
            continue
        if index + 1 < len(lines) and lines[index + 1].strip():
            continue
        out.append((label, match.group("title"), index + 1))
    return out


def _is_descendant(label: str, parent: str) -> bool:
    return label == parent or label.startswith(parent + ".")


def _section_span(
    headings: List[Tuple[str, str, int]], label: str, total: int
) -> Optional[Tuple[int, int]]:
    """The `(first_line, last_line)` of `label` and everything beneath it.

    A parent takes its descendants, matching `get_rfc_section`: asking for §7
    of a document whose content is in §7.1 and §7.2 should not return a
    heading and nothing else.

    Only the **first contiguous run** of descendants counts. `_draft_headings`
    filters out most false positives but cannot catch blank-line-separated
    pseudocode that reuses a low number, and spanning from the first match to
    the last would then stretch §1 from the introduction to somewhere near the
    end of the document — returning most of the draft under the name of one
    section. Stopping at the first heading that is *not* a descendant bounds
    the damage to what a correct parse would have returned anyway.
    """
    indices = [i for i, h in enumerate(headings) if _is_descendant(h[0], label)]
    if not indices:
        return None
    run_end = indices[0]
    for index in indices[1:]:
        if index != run_end + 1:
            break
        run_end = index
    start = headings[indices[0]][2]
    after = headings[run_end + 1][2] if run_end + 1 < len(headings) else None
    return (start, (after - 1) if after is not None else total)


def _render_draft_outline(
    fname: str, headings: List[Tuple[str, str, int]], total: int
) -> str:
    rows = []
    for index, (label, title, line) in enumerate(headings):
        nxt = headings[index + 1][2] if index + 1 < len(headings) else total + 1
        rows.append(f"| {label} | {title} | {line} | {nxt - line:,} |")
    return "\n".join(
        [
            f"# {fname} — {total:,} lines, {len(headings)} sections",
            "",
            "| Section | Title | Line | Lines |",
            "|---|---|---|---|",
            *rows,
            "",
            '_Read one with `get_draft(name, section="4.2")` — a parent label '
            "takes everything beneath it. `start_line=1` reads the document "
            "from the top instead, in pages._",
        ]
    )


def _draft_by_section(path: str, section: Optional[str], max_lines: int) -> str:
    """The outline, or one section of it. Split out from `tool_get_draft` so
    the mode selection there stays a three-way choice rather than a chain."""
    fname = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return f"Could not read `{fname}`."
    headings = _draft_headings(lines)
    if not headings:
        # Some very old drafts carry no column-0 headings at all. Falling back
        # to the window keeps the tool useful rather than reporting an empty
        # outline that reads as "this draft has no sections".
        return (
            f"_No parseable section headings in `{fname}` — reading from the "
            "top instead._\n\n" + _read_file_window(path, 1, max_lines)
        )
    if section is None:
        return _render_draft_outline(fname, headings, len(lines))
    label = normalise_section(section)
    span = _section_span(headings, label, len(lines)) if label else None
    if span is None:
        missing = (
            f"'{section}' is not a section label"
            if label is None
            else (f"`{fname}` has no section {label}")
        )
        return f"{missing}.\n\n" + _render_draft_outline(fname, headings, len(lines))
    start, end = span
    return _read_file_window(
        path,
        start,
        min(end - start + 1, max_lines),
        complete_at=end,
        section_hint=" to finish this section",
    )


def tool_get_draft(
    name: str,
    section: Optional[str] = None,
    start_line: Optional[int] = None,
    max_lines: int = _DRAFT_MAX_LINES,
) -> str:
    """The newest cached revision of draft `name`: its outline, one section of
    it, or a verbatim line window."""
    path = _find_latest_draft_file(name)
    if path is None:
        return (
            f"No cached draft matching '{name}'. The owning WG must be gathered "
            f"— {gather_suggestion(normalize_draft_name(name), purpose='to fetch it')}, "
            "or call `list_corpora` to see what is available."
        )
    capped = min(max_lines, _DRAFT_MAX_LINES)
    if start_line is not None:
        return _read_file_window(path, start_line, capped)
    return _draft_by_section(path, section, capped)


def _resolve_issue_file(
    cache: str, number: str, repo: str
) -> Tuple[Optional[str], str]:
    """Resolve a per-issue *or* per-PR file by number, within `repo` if given
    else searched across every gathered repo. Returns (path, note); path is
    None with an actionable note on a miss or an ambiguous number.

    Both trees are searched because GitHub numbers issues and pull requests
    in one sequence: a caller citing "#34" has no way to know which it is,
    and within a repo only one of the two can exist. Ambiguity is still
    possible ACROSS repos, and is reported the same way as before."""
    if repo:
        for path in (issue_path(cache, repo, number), pull_path(cache, repo, number)):
            if os.path.isfile(path):
                return path, ""
        return None, (
            f"No gathered issue or PR #{number} for repo '{repo}' in this corpus."
        )
    matches: List[Tuple[str, str]] = []
    roots = [issues_dir(cache), pulls_dir(cache)]
    if not any(os.path.isdir(root) for root in roots):
        return None, "This corpus has no gathered GitHub issues or pull requests."
    for root in roots:
        if not os.path.isdir(root):
            continue
        matches += [
            (slug, os.path.join(root, slug, f"{number}.md"))
            for slug in sorted(os.listdir(root))
            if os.path.isfile(os.path.join(root, slug, f"{number}.md"))
        ]
    if not matches:
        return None, f"No gathered issue or PR #{number} in any repo of this corpus."
    if len(matches) > 1:
        repos = ", ".join(slug for slug, _ in matches)
        return None, (
            f"#{number} exists in several gathered repos ({repos}); "
            "pass `repo` (owner/repo) to choose one."
        )
    return matches[0][1], ""


@_requires_corpus
def tool_get_issue(
    wg: str,
    number: str,
    repo: str = "",
    start_line: int = 1,
    max_lines: int = _ISSUE_MAX_LINES,
) -> str:
    path, note = _resolve_issue_file(_files_dir(wg), str(number), repo)
    if path is None:
        return _with_freshness(wg, note)
    return _with_freshness(
        wg, _read_file_window(path, start_line, min(max_lines, _ISSUE_MAX_LINES))
    )


#: What to call the per-stream state line, keyed by the doc's stream.
_STREAM_STATE_LABELS = {"ietf": "WG state", "irtf": "RG state"}

#: Human label per agenda-eligibility signal (`live_lookup.DraftStatus`).
_ELIGIBILITY_LABELS = {
    "in-wg": "in the WG — agenda-eligible",
    "in-iesg": "past the WG (IESG processing)",
    "published": "published as an RFC",
    "dead": "expired or replaced",
    "unknown": "unknown",
}


def tool_draft_status(name: str) -> str:
    """Render one draft's live Datatracker status + agenda-eligibility signal.

    Live read-path tool (lazy `live_lookup` import, gather-gated). Reports the
    draft state, the WG/stream state (where WGLC lives), the resolved IESG
    state, expiry, RFC number, and the derived in-wg / in-iesg / published /
    dead signal an agenda decision turns on.
    """
    from .. import live_lookup  # pylint: disable=import-outside-toplevel

    name = (name or "").strip()
    if not name:
        return (
            "Provide a draft name, e.g. `draft-ietf-httpbis-resumable-upload` "
            "(the version suffix is optional)."
        )

    status, fetched = live_lookup.fetch_draft_status(name)
    if status is None:
        canonical = normalize_draft_name(name)
        return (
            f"Datatracker has no document named `{canonical}`. Check the "
            "`draft-...` stem (version optional).\n\n" + live_lookup.age_stamp(fetched)
        )

    label = _ELIGIBILITY_LABELS.get(status.eligibility, status.eligibility)
    lines = [f"# {status.name}\n"]
    if status.rev:
        lines.append(f"- **Revision:** -{status.rev}")
    if status.draft_state:
        lines.append(f"- **Draft state:** {status.draft_state}")
    if status.stream_state:
        stream_label = _STREAM_STATE_LABELS.get(status.stream or "", "Stream state")
        lines.append(f"- **{stream_label}:** {status.stream_state}")
    if status.iesg_state:
        lines.append(f"- **IESG state:** {status.iesg_state}")
    if status.rfc_number:
        lines.append(f"- **RFC:** {status.rfc_number}")
    if status.intended_status:
        lines.append(f"- **Intended status:** {status.intended_status}")
    if status.expires:
        lines.append(f"- **Expires:** {status.expires[:10]}")
    lines.append(f"- **Agenda eligibility:** {label}")
    if status.note:
        lines.append(f"\n> {status.note}")
    lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def register(server: "FastMCP") -> None:
    @server.tool()
    async def draft_authors(name: str) -> str:
        """The authors/editors of a draft, with contact emails — for a
        call-for-presenters or to reach a draft's owners.

        Reads the Authors' Addresses section of the newest **cached** revision
        of the draft (across all gathered corpora); offline, no network. Each
        entry gives the name, role (author/editor), organisation, and the
        email the draft itself lists. The owning corpus (the draft's WG) must
        be gathered. These are draft-stated addresses — a chair may have a
        better working address from mail and can override.

        Args:
            name: The draft name (`draft-ietf-httpbis-resumable-upload`); the
                version suffix is optional (the newest cached revision is used).
        """
        return await _offload(tool_draft_authors, name)

    @server.tool()
    async def list_drafts(corpus: str, state: str = "") -> str:
        """List a corpus's drafts with their lifecycle state, offline from the
        cache: which are active, expired, became RFCs, were replaced, or
        withdrawn, with expiry dates. Optionally filter to one `state` slug.

        This is the corpus-wide, offline, COARSE view. It does NOT include
        WG-process state (WG Last Call, IESG evaluation) for a single draft —
        for that use `draft_status` (live, one draft by name). Adoption is
        derivable from the draft name (`draft-ietf-<wg>-` is adopted). Always
        available; `draft_status` is authoritative where the live path is on.
        """
        return await _offload(tool_list_drafts, corpus, state)

    @server.tool()
    async def get_draft(
        name: str,
        section: Optional[str] = None,
        start_line: Optional[int] = None,
        max_lines: int = 2000,
    ) -> str:
        """Verbatim text of a cached Internet-Draft by name (newest cached
        revision, across all gathered corpora).

        Use this to quote a draft's ACTUAL wording — to ground a review, a
        citation, or a contribution in primary text rather than a search
        snippet. The owning WG must be gathered.

        Three modes, and the first is the one to reach for:

        - **No arguments but `name`** — the draft's outline: every section
          with its title, start line, and length. A draft is typically 2,000
          to 4,000 lines, far past what one result can carry, so start here
          and read what you need.
        - **`section="4.2"`** — that section, in full. `4.2`, `§4.2`,
          `Section 4.2` and `Appendix B` are all accepted, and a parent label
          takes everything beneath it (`section="4"` gives §4, §4.1, §4.2…).
        - **`start_line=1`** — the document from the top, in pages, for when
          you genuinely want to read it end to end.

        **A partial result says so, at both ends, in a banner that is not part
        of the document** (`PARTIAL READ — lines 1–412 of 3920`). Reads are
        bounded by characters as well as lines so the result stays inside a
        client's limit and that banner is the only truncation in play — but
        check for it before treating what you got as the whole section.
        """
        return await _offload(tool_get_draft, name, section, start_line, max_lines)

    @server.tool()
    async def get_issue(
        corpus: str,
        number: str,
        repo: str = "",
        start_line: int = 1,
        max_lines: int = 3000,
    ) -> str:
        """Verbatim text of one GitHub issue OR pull request — opening
        description and comment / review thread — from a corpus, by number,
        as a bounded line window.

        GitHub numbers issues and PRs in one sequence, so pass the number you
        have and this resolves whichever it is; a PR additionally carries its
        merge disposition (merged by whom, into which commit), the issues it
        closes, and its review verdicts.

        Use this to quote the ACTUAL text for a citation rather than a
        search snippet. Pass `repo` (owner/repo) to disambiguate when the
        corpus tracks several repos and the number is ambiguous. Page a long
        record with `start_line` (the truncation footer says where to resume).
        """
        return await _offload(
            tool_get_issue, corpus, number, repo, start_line, max_lines
        )


def _review_rows(record: "ReviewRecord") -> List[str]:
    """The review table: one row per assignment, produced or not.

    An assignment that produced nothing has no revision and no result, so its
    row says which state it ended in instead — a directorate that returned
    nothing is a fact about the review coverage, not a blank to hide."""
    out = [
        "| Rev | Team | Type | Reviewer | Result | Date |",
        "|---|---|---|---|---|---|",
    ]
    for row in record.reviews:
        result = row.result or f"_{row.state}_"
        out.append(
            f"| {'-' + row.reviewed_rev if row.reviewed_rev else '—'} "
            f"| {row.team or '—'} | {(row.kind or '—').upper()} "
            f"| {row.reviewer} | {result} | {row.when or '—'} |"
        )
    return out


def _position_rows(record: "ReviewRecord") -> List[str]:
    """The ballot table, in the order positions were cast."""
    out = ["| Rev | Position | Balloter | Date |", "|---|---|---|---|"]
    for row in record.positions:
        position = f"**{row.position}**" if row.discuss else row.position
        out.append(
            f"| {'-' + row.rev if row.rev else '—'} | {position} "
            f"| {row.balloter} | {row.when or '—'} |"
        )
    return out


def _behind(current: Optional[int], newest: int) -> str:
    """`-07 (1 revision behind)` — the gap from the current revision."""
    label = f"-{newest:02d}"
    if current is None or current <= newest:
        return label
    gap = current - newest
    return f"{label} ({gap} revision{'' if gap == 1 else 's'} behind)"


def _spread(revs: List[int], current: Optional[int]) -> str:
    """How a set of positions distributes across revisions, newest first.

    Reported per revision rather than as a single newest: a ballot where one
    AD has re-balloted on the current text and four have not is a different
    thing from four positions on the current text, and a max hides that.
    """
    counts: Dict[int, int] = {}
    for rev in revs:
        counts[rev] = counts.get(rev, 0) + 1
    return ", ".join(
        f"{counts[rev]} against {_behind(current, rev)}"
        for rev in sorted(counts, reverse=True)
    )


def _coverage_verdict(record: "ReviewRecord") -> List[str]:
    """The lines the whole tool exists for: whether anything has examined the
    text now in front of the reader — reported per half.

    The two halves are stated separately because they answer different
    questions and routinely disagree: on a document at the IESG the reviews
    can be two revisions behind while a single AD has re-balloted on the
    current text. A combined "newest input" would report that document as
    reviewed. Mechanical throughout — it counts revisions, and says nothing
    about whether the gap matters. That judgement is the reader's.
    """
    rev_text = record.rev or ""
    current = int(rev_text) if rev_text.isdigit() else None
    label = f"-{record.rev}" if record.rev else "the current revision"
    completed = [row for row in record.reviews if row.produced_review]
    unproductive = len(record.reviews) - len(completed)
    review_revs = [int(row.reviewed_rev) for row in completed if row.reviewed_rev]
    position_revs = [int(row.rev) for row in record.positions if row.rev]

    lines: List[str] = []
    if current is not None and current not in review_revs + position_revs:
        lines.append(
            f"**Nothing in this record has examined {label}.** Text added or "
            "rewritten since has been seen by no reviewer and no balloter; "
            "`get_draft` on both revisions shows what moved."
        )
        lines.append("")
    if review_revs:
        note = (
            f" ({unproductive} assignment{'' if unproductive == 1 else 's'} "
            "produced no review)"
            if unproductive
            else ""
        )
        lines.append(f"- **Reviews:** {_spread(review_revs, current)}{note}.")
    elif record.reviews:
        lines.append(
            f"- **Reviews:** none completed — {len(record.reviews)} assignment(s), "
            "none of which produced a review."
        )
    else:
        lines.append("- **Reviews:** none requested.")
    if position_revs:
        lines.append(f"- **Ballot:** {_spread(position_revs, current)}.")
    elif record.positions:
        lines.append(
            f"- **Ballot:** {len(record.positions)} position(s), none carrying "
            "the revision they were cast against."
        )
    else:
        lines.append("- **Ballot:** not opened.")
    return lines


def tool_review_record(name: str) -> str:
    """Render one draft's review and ballot history, keyed by revision.

    Live read-path tool (lazy `live_lookup` import, gather-gated), for the
    question a document at Last Call or on a telechat turns on: has anyone
    reviewed *this* text. See `live_lookup.reviews` for why the join has to
    happen here rather than being left to the caller.
    """
    from .. import live_lookup  # pylint: disable=import-outside-toplevel

    name = (name or "").strip()
    if not name:
        return (
            "Provide a draft name, e.g. `draft-ietf-httpbis-no-vary-search` "
            "(the version suffix is optional)."
        )
    record, fetched = live_lookup.fetch_review_record(name)
    if record is None:
        canonical = normalize_draft_name(name)
        return (
            f"Datatracker has no document named `{canonical}`. Check the "
            "`draft-...` stem (version optional).\n\n" + live_lookup.age_stamp(fetched)
        )

    rev = f"-{record.rev}" if record.rev else "unknown"
    when = f" ({record.rev_date})" if record.rev_date else ""
    lines = [f"# {record.name} — review record\n", f"**Current revision:** {rev}{when}"]
    lines.append("")
    lines += _coverage_verdict(record)
    if record.reviews:
        lines += ["", f"## Reviews ({len(record.reviews)})", ""]
        lines += _review_rows(record)
    else:
        lines += ["", "## Reviews", "", "_No review has been requested._"]
    if record.positions:
        lines += ["", f"## Ballot ({len(record.positions)} positions)", ""]
        lines += _position_rows(record)
        if any(row.discuss for row in record.positions):
            # The DISCUSS text is long and is already gathered per draft; this
            # tool carries the shape of the ballot, not its bodies.
            lines.append(
                "\n_A DISCUSS holds publication. Its text is in the gathered "
                f"`ballots/{record.name}.md` — read that for the substance._"
            )
    else:
        lines += ["", "## Ballot", "", "_No ballot has been opened._"]
    lines.append("")
    lines.append(live_lookup.age_stamp(fetched))
    return "\n".join(lines)


def register_live(server: "FastMCP") -> None:
    @server.tool()
    async def draft_status(name: str) -> str:
        """**First call for where an IETF draft actually stands** — prefer
        it to web search for any "what state is draft-… in / how far along is
        it / is it in WGLC, IESG, or published" question. One draft's current
        status, **live** from Datatracker, with a derived eligibility signal.

        Returns the revision, the draft state (Active / Expired / Replaced /
        RFC), the **WG state** — the stream state the WG itself drives (`WG
        Document`, `In WG Last Call`, `WG Consensus: Waiting for Write-Up`,
        `Submitted to IESG for Publication`, …), which is where WGLC shows up
        — the IESG state (`I-D Exists`, `AD Evaluation`, `IESG Evaluation`,
        `RFC Ed Queue`, …), the expiry date, the intended status, and the RFC
        number if published — plus a derived signal: **in-wg** (still in WG
        hands), **in-iesg** (past the WG, in IESG processing), **published**,
        or **dead** (expired or replaced). A draft in WGLC is still `I-D
        Exists` on the IESG side, so read the WG state line for where it sits
        in the WG's own process. The gather cache's curated active-draft list
        can lag the real state by days, so reach here when the *current*
        standing matters (deciding an agenda is the obvious case).

        Live (short TTL + freshness stamp; it reaches the network).

        Args:
            name: The draft name (`draft-ietf-httpbis-resumable-upload`);
                the version suffix is optional.
        """
        return await _offload(tool_draft_status, name)

    @server.tool()
    async def review_record(name: str) -> str:
        """**Who has reviewed which revision of a draft** — every directorate
        review and IESG ballot position, each with the revision it was cast
        against, plus the current revision. **Live** from Datatracker.

        Call this before reviewing or commenting on a draft at WGLC, IETF Last
        Call, or on an IESG telechat. It answers the question the individual
        records cannot: *has anyone looked at the text actually in front of
        them?* A revision posted after the reviews were written is unreviewed
        text, and that reframes a review — a finding in unexamined text a week
        before a telechat is a different contribution from the same finding in
        text four reviewers cleared.

        Returns a verdict line first (whether anything has been cast against
        the current revision, and how many revisions behind the newest input
        is), then the reviews, then the ballot. **Assignments that produced no
        review are included** — rejected, or assigned and never completed —
        because a directorate that returned nothing is a fact about the
        coverage, and filtering to completed rows hides it.

        Two things this is the only way to get: Datatracker's ballot *page*
        does not show which revision a position was cast against, so scraping
        it cannot answer the question; and the review and ballot halves live
        at separate endpoints, which is a join that is easy to get wrong by
        hand (three reviews collapse into one, the -06 rows vanish).

        `draft_status` for where the draft sits in the process;
        `read_digest(kind="timeline", event_kind="ballot")` and the gathered
        `ballots/<draft>.md` for DISCUSS text and the WG's own record.

        Live (short TTL + freshness stamp; it reaches the network).

        Args:
            name: The draft name (`draft-ietf-httpbis-no-vary-search`);
                the version suffix is optional.
        """
        return await _offload(tool_review_record, name)
