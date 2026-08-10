"""Draft / issue tools: draft_authors, list_drafts, get_draft,
get_issue, draft_status."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, List, Optional, Tuple

from ..freshness import gather_suggestion
from ..gather.sources.citations import normalize_draft_name
from ..gather.sources.documents_manifest import load_documents_manifest
from ..paths import drafts_dir, issue_path, issues_dir, pull_path, pulls_dir
from .common import _files_dir, _list_wgs, _offload, _requires_corpus, _with_freshness

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP  # pragma: no cover


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


def _read_file_window(path: str, start_line: int, max_lines: int) -> str:
    """Return a bounded, header-stamped line window of `path`, with a footer
    pointing at how to page further when it is truncated."""
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
    end = min(total, start - 1 + max(1, max_lines))
    header = f"# {os.path.basename(path)} (lines {start}–{end} of {total})\n\n"
    footer = ""
    if end < total:
        footer = (
            f"\n\n_(showing lines {start}–{end} of {total}; continue with "
            f"`start_line={end + 1}`)_\n"
        )
    return header + "".join(lines[start - 1 : end]) + footer


def tool_get_draft(
    name: str, start_line: int = 1, max_lines: int = _DRAFT_MAX_LINES
) -> str:
    """Verbatim text of the newest cached revision of draft `name`, bounded."""
    path = _find_latest_draft_file(name)
    if path is None:
        return (
            f"No cached draft matching '{name}'. The owning WG must be gathered "
            f"— {gather_suggestion(normalize_draft_name(name), purpose='to fetch it')}, "
            "or call `list_corpora` to see what is available."
        )
    return _read_file_window(path, start_line, min(max_lines, _DRAFT_MAX_LINES))


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
    async def get_draft(name: str, start_line: int = 1, max_lines: int = 2000) -> str:
        """Verbatim text of a cached Internet-Draft by name (newest cached
        revision, across all gathered corpora), as a bounded line window.

        Use this to quote a draft's ACTUAL wording — to ground a review, a
        citation, or a contribution in primary text rather than a search
        snippet. Page a long draft with `start_line`. The owning WG must be
        gathered.
        """
        return await _offload(tool_get_draft, name, start_line, max_lines)

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
