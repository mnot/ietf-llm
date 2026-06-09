"""Compose a tight first-call overview of a WG.

The agent's most common opening move is "tell me about this WG."
Reading four full digests to answer that question burns ~80-100KB
of context for data the agent will mostly never reference again.

`build_overview(wg, cache_dir)` returns a ~30-line markdown summary
that answers the structural questions — who runs the WG, what
documents are active, what's the most recent activity, where to
look next — in one tool call. Composed from the existing digests
via `digest_query.query_digest`, so it always reflects the current
on-disk state and respects the same filter semantics.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional, Tuple

from ..freshness import gather_suggestion
from ..gather.documents_manifest import load_documents_manifest
from ..paths import ballots_dir, charter_path, group_path, threads_dir
from .query import (
    is_ballot_position,
    is_idaction_publication,
    parse_md_tables,
    query_digest,
)


def _digest_path(cache_dir: str, wg: str, kind: str) -> str:  # noqa: ARG001
    # Post-reorg: digests live at `digests/<kind>.md` under cache_dir,
    # with no per-WG prefix (the cache dir is already WG-specific).
    return os.path.join(cache_dir, "digests", f"{kind}.md")


def _section_lines(path: str, heading_prefix: str) -> List[str]:
    """Return the body lines of the first section whose heading starts
    with `heading_prefix`. Empty list if not found."""
    if not os.path.isfile(path):
        return []
    out: List[str] = []
    capturing = False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.rstrip("\n")
            if stripped.startswith("## "):
                if capturing:
                    break
                if stripped.startswith(f"## {heading_prefix}"):
                    capturing = True
                    continue
            if capturing:
                out.append(stripped)
    # Trim leading/trailing blanks.
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def _top_n_table_rows(path: str, heading_prefix: str, limit: int) -> List[List[str]]:
    """Pull rows from the first matching section's first table."""
    body = "\n".join(_section_lines(path, heading_prefix))
    if not body:
        return []
    sections = parse_md_tables(body)
    if not sections:
        return []
    return sections[0].rows[:limit]


def _leadership_summary(cache_dir: str, wg: str) -> str:
    """One-line "Chairs: X, Y · AD: Z" derived from the people digest."""
    rows = _top_n_table_rows(
        _digest_path(cache_dir, wg, "people"),
        "Working Group leadership",
        limit=10,
    )
    chairs: List[str] = []
    ads: List[str] = []
    other: List[str] = []
    for row in rows:
        if len(row) < 2:
            continue
        role = row[0]
        name = row[1]
        if "Chair" in role:
            chairs.append(name)
        elif "Area Director" in role:
            ads.append(name)
        else:
            other.append(f"{role}: {name}")
    parts: List[str] = []
    if chairs:
        parts.append("**Chairs:** " + ", ".join(chairs))
    if ads:
        parts.append("**AD:** " + ", ".join(ads))
    parts.extend(other)
    return " · ".join(parts) if parts else "_(no leadership recorded)_"


#: Matches a published-RFC document name (`rfc9110`), case-insensitive.
_RFC_RE = re.compile(r"^rfc\d+$", re.IGNORECASE)


class _DocSummary(NamedTuple):
    """Split view of a WG's documents for the overview."""

    active_drafts: List[str]  # one full author/citation bullet each
    concluded_draft_count: int  # expired / replaced / published drafts
    rfc_count: int
    rfc_lines: List[str]  # compact published-RFC body


def _documents_summary(cache_dir: str, wg: str) -> _DocSummary:
    """Split the WG's documents into active Internet-Draft bullets,
    a count of concluded drafts, and a compact published-RFC summary.

    Active drafts (a future Datatracker expiry, from `documents.json`) get
    one bullet each (authors + citation count). Concluded drafts (expired /
    replaced / published — a past expiry) and RFCs are finished work and
    low-signal for orientation, so they collapse to counts rather than
    burying the live drafts. Drafts absent from the manifest (individual or
    freshly-added drafts, or a cache gathered before expiry was recorded)
    default to active, so nothing tracked is ever silently hidden.
    """
    rows = _top_n_table_rows(
        _digest_path(cache_dir, wg, "people"),
        "Document authors",
        limit=50,
    )
    citation_counts = _load_citation_counts(cache_dir)
    manifest = load_documents_manifest(wg)
    now = datetime.now(timezone.utc)
    # The table is Name | Documents | Email; we want one entry per
    # distinct document with its authors gathered alongside.
    docs: dict[str, List[str]] = {}
    for row in rows:
        if len(row) < 2:
            continue
        name = row[0]
        document_cell = row[1]
        for entry in [d.strip() for d in document_cell.split(",")]:
            if not entry:
                continue
            # "draft-foo (ed.)" → key "draft-foo", role "editor"
            match = re.match(r"^(?P<doc>[^\s(]+)\s*(?P<role>\(.*\))?$", entry)
            if not match:
                continue
            doc = match.group("doc")
            tagged = (
                f"{name} {match.group('role')}".strip() if match.group("role") else name
            )
            docs.setdefault(doc, []).append(tagged)

    active_drafts: List[str] = []
    concluded_drafts = 0
    # RFC names come from the documents manifest (recorded at gather time
    # from the global series index) unioned with any RFCs still in the
    # author table — the latter covers a cache gathered before RFC bodies
    # were dropped, so it keeps its RFC listing until the next gather.
    # Bodies are no longer required on disk.
    rfc_names = {name for name in manifest if _RFC_RE.match(name)}
    for doc, authors in sorted(docs.items()):
        if _RFC_RE.match(doc):
            rfc_names.add(doc)
            continue
        if _draft_concluded(doc, manifest, now):
            concluded_drafts += 1
            continue
        cited = citation_counts.get(doc.lower()) or 0
        cite_tag = f"  _(cited in {cited})_" if cited else ""
        active_drafts.append(f"- `{doc}` — {', '.join(sorted(authors))}{cite_tag}")
    rfcs = sorted((name, citation_counts.get(name.lower()) or 0) for name in rfc_names)
    return _DocSummary(
        active_drafts, concluded_drafts, len(rfcs), _rfc_summary_lines(rfcs)
    )


#: A `**Tally:** … N DISCUSS …` count on a ballot file.
_TALLY_DISCUSS_RE = re.compile(r"(\d+)\s+DISCUSS", re.IGNORECASE)


def _blocked_drafts(cache_dir: str) -> List[Tuple[str, int]]:
    """Drafts with an unresolved IESG DISCUSS, read from the `**Tally:**`
    line of each `ballots/<draft>.md`. A DISCUSS holds publication, so this
    is the substance a "what is going on" answer most needs to read — the
    overview flags it and points at the ballot. Returns
    `[(draft-name, discuss_count), …]` sorted by name."""
    bdir = ballots_dir(cache_dir)
    if not os.path.isdir(bdir):
        return []
    out: List[Tuple[str, int]] = []
    for fname in sorted(os.listdir(bdir)):
        if not (fname.startswith("draft-") and fname.endswith(".md")):
            continue
        try:
            with open(os.path.join(bdir, fname), "r", encoding="utf-8") as fh:
                head = fh.read(2000)  # the tally line is near the top
        except OSError:
            continue
        for line in head.splitlines():
            if line.startswith("**Tally:**"):
                match = _TALLY_DISCUSS_RE.search(line)
                if match and int(match.group(1)) > 0:
                    out.append((fname[:-3], int(match.group(1))))
                break
    return out


def _draft_concluded(
    doc: str, manifest: "dict[str, dict[str, str | None]]", now: datetime
) -> bool:
    """True when the manifest records a *past* expiry for `doc` (expired /
    replaced / published). Unknown drafts (not in the manifest) are treated
    as active, so individual or freshly-added drafts are never hidden."""
    rec = manifest.get(doc)
    if not rec:
        return False
    raw = rec.get("expires")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < now


def _rfc_summary_lines(rfcs: "List[tuple[str, int]]") -> List[str]:
    """Compact body for the published-RFC section: the currently-cited
    RFCs inline (with counts), the dormant rest as a count + pointer.
    Empty when there are no RFCs."""
    if not rfcs:
        return []
    cited = sorted((r for r in rfcs if r[1]), key=lambda r: (-r[1], r[0]))
    if cited:
        rendered = ", ".join(f"`{doc}` _(cited in {n})_" for doc, n in cited)
        rest = len(rfcs) - len(cited)
        tail = f" {rest} more in `drafts/` (see `list_files`)." if rest else ""
        return [f"Cited in current discussion: {rendered}.{tail}"]
    return ["_None currently cited in corpus discussion; all in `drafts/`._"]


def _load_citation_counts(cache_dir: str) -> dict[str, int]:
    """Read the `citations.md` digest and return `{draft → count}`.

    Parses the `## \\`draft-foo\\` (N citation(s))` headers — cheap
    regex over the digest's structured headings. Empty dict when no
    citations digest exists (gather pre-citations or no citations
    found).
    """
    path = _digest_path(cache_dir, "wg", "citations")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for match in re.finditer(
        r"^## `(?P<doc>[^`]+)` \((?P<n>\d+) citation",
        text,
        re.MULTILINE,
    ):
        try:
            out[match.group("doc").lower()] = int(match.group("n"))
        except ValueError:
            continue
    return out


#: The columns the overview's open-issues table declares, by lowercased name.
#: The issues digest carries more (Participants, Dup-of, File, Summary); rows
#: are projected down to these or the wider rows spill past the 7-column header
#: and break the rendered table.
_OPEN_ISSUE_COLUMNS = ["#", "state", "title", "labels", "comments", "updated", "author"]


def _recent_open_issues(cache_dir: str, wg: str, limit: int) -> List[List[str]]:
    """Filter the issues digest to the most recently updated open ones,
    projected to the columns the overview table shows (`_OPEN_ISSUE_COLUMNS`)."""
    issues_path = _digest_path(cache_dir, wg, "issues")
    if not os.path.isfile(issues_path):
        return []
    filtered_md = query_digest(issues_path, "issues", state="open", limit=limit)
    rows: List[List[str]] = []
    for section in parse_md_tables(filtered_md):
        cols = [c.lower() for c in section.columns]
        idx = [cols.index(n) if n in cols else None for n in _OPEN_ISSUE_COLUMNS]
        for row in section.rows:
            rows.append([row[i] if i is not None and i < len(row) else "" for i in idx])
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    return rows[:limit]


def _group_facts(cache_dir: str) -> "tuple[Optional[str], Optional[str], List[str]]":
    """Read `group.md`: `(status_line, area_line, resource_bullets)`.

    status / area are the literal `**Status:** …` / `**Area:** …`
    lines (or None); resource_bullets are the `- label: url` lines from
    the Resources section. Everything empty when group.md is absent.
    """
    path = group_path(cache_dir)
    status: Optional[str] = None
    area: Optional[str] = None
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("**Status:**"):
                    status = line.strip()
                elif line.startswith("**Area:**"):
                    area = line.strip()
                elif line.startswith("## "):
                    break
    return status, area, _section_lines(path, "Resources")


#: A line of only rule characters (`====`, `----`), as our charter
#: writer emits under the header. Skipped when picking the excerpt.
_RULE_LINE_RE = re.compile(r"^[=_-]{3,}$")
#: The header lines our charter writer prepends.
_CHARTER_HEADER_RE = re.compile(r"^\s*(Working Group Charter:|Source:)", re.IGNORECASE)


def _charter_excerpt(
    cache_dir: str,
    max_chars: int = 600,
) -> Optional[str]:
    """First non-empty paragraph of the WG's charter, capped at
    `max_chars`. Returns None if the charter file doesn't exist or is
    empty. Strips leading boilerplate that some charters carry
    (procedural headers, status lines) by finding the first paragraph
    that's substantively long enough to be the mission statement.
    """
    path = charter_path(cache_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    paragraphs = re.split(r"\n\s*\n", text.strip())
    for raw in paragraphs:
        # Drop the boilerplate our charter writer prepends — the
        # `Working Group Charter:` / `Source:` lines and the `===` rule —
        # before measuring. They carry no blank line between them, and
        # the rule alone is 80 chars, so without this the header block
        # clears the length gate and gets returned as the "excerpt".
        kept = [
            ln
            for ln in raw.splitlines()
            if not _RULE_LINE_RE.match(ln.strip()) and not _CHARTER_HEADER_RE.match(ln)
        ]
        para = " ".join(" ".join(kept).split())  # collapse whitespace
        # Skip headers / short procedural lines; the mission paragraph
        # is typically several sentences.
        if len(para) < 80:
            continue
        if len(para) > max_chars:
            para = para[: max_chars - 1].rstrip() + "…"
        return para
    return None


def _subject_prefix_frequencies(cache_dir: str) -> List[tuple[str, int]]:
    """Count `[xxx]`-style subject prefixes across per-thread files.

    Many WGs (TLS being the canonical example) don't use GitHub
    issue labels — they cluster topics on the mailing list via
    bracketed subject prefixes: `[mlkem] consensus call`,
    `[ech] negotiation`. This gives consumers the same shape of
    curation vocabulary `list_labels` provides for GitHub-driven
    WGs, derived from data the WG actually maintains.

    Walks the per-thread `.md` files under `threads/`, pulls each
    section header's `_Subject:_` line, strips the standard
    `Re:` / `Fwd:` chrome plus the WG-name prefix
    (`[TLS]`, `[httpbis]`, …), then counts remaining `[xxx]`
    tokens. The WG's own acronym is hard-coded as noise — the
    *topic-cluster* prefixes are what carry signal.

    Returns `[(prefix, n), ...]` sorted by count descending. The
    prefix is rendered lowercased and bracket-wrapped (`[mlkem]`)
    to match how a consumer would type it into
    `read_digest(kind="threads", subject="[mlkem]")`.
    """
    threads = threads_dir(cache_dir)
    if not os.path.isdir(threads):
        return []
    counts: dict[str, int] = {}
    # Match `_Subject:_ <text>` lines inside the per-thread files.
    # The text after the marker is the original (un-normalised)
    # subject of that specific message section.
    subj_line_re = re.compile(r"^_Subject:_\s*(.+)$", re.MULTILINE)
    # The leading-junk pattern from text.py, but capturing the
    # bracketed tokens instead of stripping them.
    bracket_re = re.compile(r"\[([^\]]+)\]")
    re_fwd_re = re.compile(r"^\s*(?:re|fwd|fw|aw|sv)\s*:\s*", re.IGNORECASE)
    for name in os.listdir(threads):
        if not name.endswith(".md"):
            continue
        try:
            with open(
                os.path.join(threads, name),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as fh:
                text = fh.read()
        except OSError:
            continue
        for match in subj_line_re.finditer(text):
            subj = match.group(1).strip()
            # Strip Re:/Fwd: prefixes iteratively (subjects may have
            # multiple after long threads).
            while True:
                stripped = re_fwd_re.sub("", subj)
                if stripped == subj:
                    break
                subj = stripped
            for token_match in bracket_re.finditer(subj):
                token = token_match.group(1).strip().lower()
                if not token:
                    continue
                # A bracket past a non-bracket word isn't a prefix —
                # stop at the first non-bracket span. (We check by
                # looking at what comes before this match.)
                pre = subj[: token_match.start()].strip()
                if pre and not pre.endswith("]"):
                    break
                counts[f"[{token}]"] = counts.get(f"[{token}]", 0) + 1
    # Sort by count desc, then prefix asc.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _label_frequencies(cache_dir: str, wg: str) -> List[tuple[str, int]]:
    """Count distinct issue labels across the issues digest.

    Returns `[(label, n), ...]` sorted by frequency descending. The point
    is to give a consuming LLM a frequency-based glossary so it can
    weight hits faster and explain unfamiliar labels (`"wglc"`, `"ready
    to close"`, …) by association. Labels are stored as `"a, b, c"` in
    the digest's Labels column; we just split and count.
    """
    issues_path = _digest_path(cache_dir, wg, "issues")
    if not os.path.isfile(issues_path):
        return []
    with open(issues_path, encoding="utf-8") as fh:
        sections = parse_md_tables(fh.read())
    counts: dict[str, int] = {}
    for section in sections:
        # Locate the "Labels" column by header so we don't depend on
        # ordering (the issues digest schema has shifted over time).
        try:
            label_idx = section.columns.index("Labels")
        except ValueError:
            continue
        for row in section.rows:
            if label_idx >= len(row):
                continue
            for label in (lbl.strip() for lbl in row[label_idx].split(",")):
                if not label:
                    continue
                counts[label] = counts.get(label, 0) + 1
    # Sort by count desc, then label asc for stable output.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _recent_threads(cache_dir: str, wg: str, limit: int) -> Tuple[List[List[str]], str]:
    """Recent threads plus the actual label of the 6th column. The threads
    writer emits `File` (no summariser) or `Summary` (summariser active) there;
    returning the real label lets the caller's header match the cell contents
    instead of hardcoding `File`."""
    threads_path = _digest_path(cache_dir, wg, "threads")
    if not os.path.isfile(threads_path):
        return [], "File"
    filtered_md = query_digest(threads_path, "threads", limit=limit)
    rows: List[List[str]] = []
    last_label = "File"
    for section in parse_md_tables(filtered_md):
        if len(section.columns) >= 6:
            last_label = section.columns[5]
        rows.extend(section.rows)
        if len(rows) >= limit:
            break
    return rows[:limit], last_label


def _iso_date(cell: str) -> str:
    """Extract a YYYY-MM-DD prefix from a table cell, or ""."""
    match = re.search(r"\d{4}-\d{2}-\d{2}", cell)
    return match.group(0) if match else ""


def _recent_window_floor(threads_path: str, window_days: int) -> str:
    """The since-date `window_days` before the latest thread activity, so
    "most active" means *recently* active (robust to how long ago the
    gather ran). Empty when no dated rows."""
    try:
        with open(threads_path, "r", encoding="utf-8") as fh:
            sections = parse_md_tables(fh.read())
    except OSError:
        return ""
    latest = ""
    for section in sections:
        cols = [c.lower() for c in section.columns]
        if "last" not in cols:
            continue
        i_last = cols.index("last")
        for row in section.rows:
            if i_last < len(row):
                latest = max(latest, _iso_date(row[i_last]))
    if not latest:
        return ""
    try:
        return (
            datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=window_days)
        ).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _most_active_threads(
    cache_dir: str, wg: str, limit: int = 5, window_days: int = 31
) -> List[List[str]]:
    """Recently-active threads ranked by message count — the "where is the
    back-and-forth" signal, distinct from the recency list. Reuses the
    general `read_digest` heat query (`sort="activity"`, `min_messages=2`
    to drop single-message posts, `since=` the recent window) so the
    overview and a direct client call rank identically. Returns
    `[[subject, msgs, participants, last], …]`, hottest first.
    """
    threads_path = _digest_path(cache_dir, wg, "threads")
    if not os.path.isfile(threads_path):
        return []
    floor = _recent_window_floor(threads_path, window_days)
    md = query_digest(
        threads_path,
        "threads",
        sort="activity",
        since=floor or None,
        min_messages=2,
        limit=limit,
    )
    rows: List[List[str]] = []
    for section in parse_md_tables(md):
        cols = [c.lower() for c in section.columns]
        if "subject" not in cols:
            continue
        i_subj = cols.index("subject")
        i_msgs = cols.index("msgs") if "msgs" in cols else None
        i_part = cols.index("participants") if "participants" in cols else None
        i_last = cols.index("last") if "last" in cols else None
        for row in section.rows:
            if i_subj >= len(row):
                continue
            rows.append(
                [
                    row[i_subj],
                    row[i_msgs] if i_msgs is not None and i_msgs < len(row) else "",
                    row[i_part] if i_part is not None and i_part < len(row) else "",
                    (
                        _iso_date(row[i_last])
                        if i_last is not None and i_last < len(row)
                        else ""
                    ),
                ]
            )
    return rows[:limit]


class _RecentActivity(NamedTuple):
    """Recent timeline activity, with mechanical events folded to counts."""

    events: List[str]  # discussion / decision events, newest first
    idaction: int  # automated I-D Action draft publications
    ballots: int  # individual IESG ballot positions


def _recent_activity(cache_dir: str, wg: str, limit: int) -> _RecentActivity:
    """The recent discussion / decision events (WGLCs, adoption calls,
    meetings, RFC milestones), newest first, with the two mechanical event
    classes folded into counts so they do not crowd out the human signal:

    - **I-D Action publications** (`… published · \\`threads/…\\``) — robot
      announcements of new draft revisions.
    - **IESG ballot positions** (`… → No Objection · ballots/…`) — routine
      per-AD clearing; a single contested draft can flood the list with a
      dozen. The one position that matters, a DISCUSS, is surfaced by the
      blocked-drafts section above, so folding the rest loses nothing.

    An RFC milestone (`… published as RFC`) is kept — it is a real event,
    not a routine revision.
    """
    timeline_path = _digest_path(cache_dir, wg, "timeline")
    if not os.path.isfile(timeline_path):
        return _RecentActivity([], 0, 0)
    filtered = query_digest(timeline_path, "timeline", limit=max(limit * 4, 40))
    rows = [
        line[2:].strip() for line in filtered.splitlines() if line.startswith("- **")
    ]
    events: List[str] = []
    idaction = 0
    ballots = 0
    for row in rows:
        if is_idaction_publication(row):
            idaction += 1
        elif is_ballot_position(row):
            ballots += 1
        elif len(events) < limit:
            events.append(row)
    return _RecentActivity(events, idaction, ballots)


# --- Public entry point ---------------------------------------------------


def build_overview(wg: str, cache_dir: str) -> str:
    """Return a ~30-line markdown overview of the WG.

    Composes from the existing digest files; no network, no model.
    Cheap to call.
    """
    if not os.path.isdir(cache_dir):
        return (
            f"No cache for {wg} — "
            f"{gather_suggestion(wg, purpose='first to gather materials')}."
        )

    out: List[str] = []
    out.append(f"# {wg} — overview\n")

    # Freshness (the gather date, escalating to a refresh prompt when
    # stale) is prepended by the MCP layer's `_with_freshness` on every
    # top-level response, so the overview itself no longer repeats it.

    out.append("## Working Group")
    out.append(_leadership_summary(cache_dir, wg))
    # Status / area give the consumer orientation: a concluded WG
    # won't see new activity; the area says where it sits in the org.
    status, area, resources = _group_facts(cache_dir)
    if status:
        out.append(status)
    if area:
        out.append(area)
    # Charter is the authoritative statement of scope and goals;
    # consumers answering "is X in scope" or "what does the WG
    # actually do" need the literal text. Render an excerpt inline
    # so the consumer sees the mission statement without a follow-up
    # read — most scope debates can be answered from the first
    # paragraph alone. Capped so this stays under the overview's
    # ~30-line budget.
    charter = _charter_excerpt(cache_dir)
    if charter is not None:
        out.append("**Charter excerpt** (first paragraph; full text in `charter.txt`):")
        out.append("")
        # Render as a blockquote so the excerpt is visually distinct
        # from overview's own narrative copy.
        for line in charter.splitlines():
            out.append(f"> {line}")
    out.append("")

    # Additional Resources (repos / home page / chat / archives) —
    # the "where do I go next" links, straight from the group record.
    if resources:
        out.append("## Resources")
        out.extend(resources)
        out.append("")

    docs = _documents_summary(cache_dir, wg)
    if docs.active_drafts or docs.concluded_draft_count:
        out.append(f"## Internet-Drafts ({len(docs.active_drafts)} active)")
        if any("cited in" in bullet for bullet in docs.active_drafts):
            out.append(
                '_"cited in N" = distinct threads / issues across the gathered '
                "corpus that reference the draft (de-duplicated per source); "
                "cumulative, not weighted by recency. `find_citations` lists "
                "them._"
            )
        out.extend(docs.active_drafts)
        if docs.concluded_draft_count:
            out.append(
                f"_+ {docs.concluded_draft_count} expired or concluded "
                f"draft(s), in `drafts/`._"
            )
        out.append("")
    if docs.rfc_count:
        out.append(f"## Published RFCs ({docs.rfc_count})")
        out.extend(docs.rfc_lines)
        out.append("")

    blocked = _blocked_drafts(cache_dir)
    if blocked:
        out.append(f"## ⚠ Blocked on IESG DISCUSS ({len(blocked)})")
        out.append(
            "_A DISCUSS holds publication until cleared with the responsible "
            "AD. Read the ballot for the actual objection — a later list / "
            "chair discussion may already have addressed it._"
        )
        for name, count in blocked:
            noun = "DISCUSS" if count == 1 else "DISCUSSes"
            out.append(f"- `{name}` — {count} {noun}; read `ballots/{name}.md`.")
        out.append("")

    labels = _label_frequencies(cache_dir, wg)
    if labels:
        # Top labels by frequency — a poor-man's glossary. A consuming
        # LLM that sees "top-level (8)" or "wglc (3)" can infer the
        # label's role from its frequency + context. We cap at 12 to
        # keep this section bounded; the full label set is in the
        # issues digest.
        top = labels[:12]
        out.append(f"## Top issue labels ({len(labels)} total)")
        out.append(
            ", ".join(f"`{label}` ({count})" for label, count in top)
            + ("." if len(labels) <= len(top) else " — _and others._")
        )
        out.append("")

    open_issues = _recent_open_issues(cache_dir, wg, limit=5)
    if open_issues:
        out.append("## 5 most-recently-updated open issues")
        out.append("| # | State | Title | Labels | Comments | Updated | Author |")
        out.append("|---|-------|-------|--------|----------|---------|--------|")
        for row in open_issues:
            out.append("| " + " | ".join(row) + " |")
        out.append("")

    # Prefer the heat signal (where the back-and-forth is) over a pure
    # recency list — a consumer asking "what is going on" wants conflict,
    # not the newest single-message post. Fall back to recency only when
    # nothing has multi-message activity in the window.
    active_threads = _most_active_threads(cache_dir, wg, limit=5)
    if active_threads:
        out.append("## Most active threads recently (by message count)")
        out.append(
            "_Where the back-and-forth is — ranked by messages in a recent "
            "window, not by recency. Read the thread for the substance._"
        )
        out.append("| Subject | Msgs | Participants | Last |")
        out.append("|---|---|---|---|")
        for row in active_threads:
            out.append("| " + " | ".join(row) + " |")
        out.append("")
    else:
        recent_threads, last_label = _recent_threads(cache_dir, wg, limit=5)
        if recent_threads:
            out.append("## 5 most recent mailing list threads")
            out.append(
                f"| Subject | Msgs | Participants | First | Last | {last_label} |"
            )
            out.append("|---|---|---|---|---|---|")
            for row in recent_threads:
                out.append("| " + " | ".join(row) + " |")
            out.append("")

    activity = _recent_activity(cache_dir, wg, limit=10)
    if activity.events or activity.idaction or activity.ballots:
        out.append("## Recent activity")
        out.extend(f"- {event}" for event in activity.events)
        folded = []
        if activity.idaction:
            folded.append(f"{activity.idaction} I-D Action draft publication(s)")
        if activity.ballots:
            folded.append(f"{activity.ballots} IESG ballot position(s)")
        if folded:
            out.append(
                f"_+ {', '.join(folded)} in this span (mechanical, folded; the "
                "timeline digest has them all)._"
            )
        out.append("")

    out.append("---")
    out.append("")
    out.append("## Where to look next")
    out.append("")
    out.append(
        "Pick by **question shape**, not by tool — this overview is for "
        "orienting only:"
    )
    out.append("")
    out.append(
        '- _"arguments for/against X"_ / _"scope debate about X"_ → '
        f'`search_corpus("{wg}", "X", label="...")` '
        f'(labels are the WG\'s own curation; call `list_labels("{wg}")` '
        "first if you don't know the vocabulary)."
    )
    out.append(
        '- _"what did the WG decide about X?"_ / _"WG\'s position on X?"_ → '
        f'`search_corpus("{wg}", "X", state="closed")` '
        "(the chairs' resolution lives in closed issues)."
    )
    out.append(
        '- _"what was said about X?"_ → '
        f'`search_corpus("{wg}", "X")`, then pivot with '
        "`get_chunk_text` / `read_file_section` for full context."
    )
    out.append(
        '- _"how did the debate on X evolve?"_, _"walk me through the '
        'discussion of Y"_ → '
        f'`read_topic("{wg}", "X")` '
        "(returns full messages in date order across threads and "
        "issues; the narrative-arc primitive)."
    )
    out.append(
        '- _"the WG supported / opposed X"_, _"who\'s in which camp?"_ → '
        "this corpus does **not** measure support (no vote, no sentiment "
        "score). Call `read_ietf_interpretation_norms`, then read the "
        f'chair\'s words — `search_corpus("{wg}", "X", role="Chair")` and '
        "closed-issue resolutions."
    )
    out.append(
        '- _"did the chair call consensus / was X decided?"_ → '
        "`read_ietf_interpretation_norms` (consensus is chair-declared, not "
        f'counted), then `search_corpus("{wg}", "X", role="Chair", '
        'state="closed")` and the **Chair statements** section of '
        f'`tally_positions("{wg}", "<thread or issue file>")`. Treat that '
        "tool's +1/-1 count as a keyword heuristic, never as a support level."
    )
    out.append(
        '- _"<person> opposed / objected to X"_ → cite that person\'s own '
        f'message — `search_corpus("{wg}", "X", author="...")` — not a '
        "narrative snippet; a technical concern is not a stated objection."
    )
    out.append(
        '- _"what\'s open / closed?"_, _"who\'s a chair?"_, '
        '_"what happened in May?"_ → '
        f'`read_digest("{wg}", kind=..., ...filters)` '
        "(kinds: `issues`, `threads`, `people`, `timeline`, `index`)."
    )
    return "\n".join(out) + "\n"
