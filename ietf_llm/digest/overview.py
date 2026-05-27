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
from typing import List, Optional

from .query import parse_md_tables, query_digest


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


def _documents_summary(cache_dir: str, wg: str) -> List[str]:
    """One bullet per active document from the people digest."""
    rows = _top_n_table_rows(
        _digest_path(cache_dir, wg, "people"),
        "Document authors",
        limit=50,
    )
    # The table is Name | Documents | Email; we want one bullet per
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
                f"{name} {match.group('role')}".strip()
                if match.group("role")
                else name
            )
            docs.setdefault(doc, []).append(tagged)
    return [
        f"- `{doc}` — {', '.join(sorted(authors))}"
        for doc, authors in sorted(docs.items())
    ]


def _recent_open_issues(cache_dir: str, wg: str, limit: int) -> List[List[str]]:
    """Filter the issues digest to the most recently updated open ones."""
    issues_path = _digest_path(cache_dir, wg, "issues")
    if not os.path.isfile(issues_path):
        return []
    filtered_md = query_digest(issues_path, "issues", state="open", limit=limit)
    sections = parse_md_tables(filtered_md)
    rows: List[List[str]] = []
    for section in sections:
        rows.extend(section.rows)
        if len(rows) >= limit:
            break
    return rows[:limit]


def _charter_excerpt(
    cache_dir: str, max_chars: int = 600,
) -> Optional[str]:
    """First non-empty paragraph of the WG's charter, capped at
    `max_chars`. Returns None if the charter file doesn't exist or is
    empty. Strips leading boilerplate that some charters carry
    (procedural headers, status lines) by finding the first paragraph
    that's substantively long enough to be the mission statement.
    """
    # pylint: disable=import-outside-toplevel
    from ..paths import charter_path
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
        para = " ".join(raw.split())  # collapse whitespace
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
    # pylint: disable=import-outside-toplevel
    from ..paths import threads_dir
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
                os.path.join(threads, name), "r", encoding="utf-8",
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


def _recent_threads(cache_dir: str, wg: str, limit: int) -> List[List[str]]:
    threads_path = _digest_path(cache_dir, wg, "threads")
    if not os.path.isfile(threads_path):
        return []
    filtered_md = query_digest(threads_path, "threads", limit=limit)
    sections = parse_md_tables(filtered_md)
    rows: List[List[str]] = []
    for section in sections:
        rows.extend(section.rows)
        if len(rows) >= limit:
            break
    return rows[:limit]


def _latest_event(
    cache_dir: str, wg: str, event_kind: str
) -> Optional[str]:
    """Return the most recent bullet of the given event kind, or None."""
    timeline_path = _digest_path(cache_dir, wg, "timeline")
    if not os.path.isfile(timeline_path):
        return None
    filtered = query_digest(
        timeline_path, "timeline", event_kind=event_kind, limit=1
    )
    for line in filtered.splitlines():
        if line.startswith("- **"):
            return line[2:].strip()
    return None


# --- Public entry point ---------------------------------------------------


def build_overview(wg: str, cache_dir: str) -> str:
    """Return a ~30-line markdown overview of the WG.

    Composes from the existing digest files; no network, no model.
    Cheap to call.
    """
    if not os.path.isdir(cache_dir):
        return (
            f"No cache for {wg}. "
            f"Run `ietf-llm {wg}` first to gather materials."
        )

    out: List[str] = []
    out.append(f"# {wg} — overview\n")

    # Freshness signal right under the title. Without this, a
    # consumer reads dates inside the overview (latest event,
    # most-recent thread) and can't tell whether the corpus is
    # current — recent feedback flagged exactly this: "latest events"
    # was dated 2025-11-02 while threads were from May 2026, and the
    # consumer didn't know which was the floor on their view.
    # pylint: disable=import-outside-toplevel
    from ..freshness import last_gathered
    gathered_at = last_gathered(wg)
    if gathered_at is not None:
        out.append(
            f"_Corpus last gathered: **{gathered_at.strftime('%Y-%m-%d')}**. "
            f"Run `ietf-llm {wg}` to refresh._\n"
        )

    out.append("## Working Group")
    out.append(_leadership_summary(cache_dir, wg))
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

    docs = _documents_summary(cache_dir, wg)
    if docs:
        out.append(f"## Documents ({len(docs)})")
        out.extend(docs)
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

    recent_threads = _recent_threads(cache_dir, wg, limit=5)
    if recent_threads:
        out.append("## 5 most recent mailing list threads")
        out.append("| Subject | Msgs | Participants | First | Last | File |")
        out.append("|---|---|---|---|---|---|")
        for row in recent_threads:
            out.append("| " + " | ".join(row) + " |")
        out.append("")

    latest_meeting = _latest_event(cache_dir, wg, "meeting")
    latest_draft = _latest_event(cache_dir, wg, "draft-published")
    if latest_meeting or latest_draft:
        out.append("## Latest events")
        if latest_meeting:
            out.append(f"- {latest_meeting}")
        if latest_draft:
            out.append(f"- {latest_draft}")
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
        f"(labels are the WG's own curation; call `list_labels(\"{wg}\")` "
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
        '- _"what\'s open / closed?"_, _"who\'s a chair?"_, '
        '_"what happened in May?"_ → '
        f'`read_digest("{wg}", kind=..., ...filters)` '
        "(kinds: `issues`, `threads`, `people`, `timeline`, `index`)."
    )
    return "\n".join(out) + "\n"
