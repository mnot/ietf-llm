"""Per-issue Markdown files for GitHub issues — symmetric with the
per-thread files written by `mail_threads`.

The existing `<wg>-github-<repo>.txt` file dumps every issue + every
comment into one multi-MB blob. An agent reading the issues digest
can see the issue's author and a comment count, but to learn who
commented (let alone what they said), it has to read the giant
text file. The threads digest already had this problem solved — per-
conversation `.md` files with structured headers — so we apply the
same pattern here.

For each issue we emit:

  <cache>/files/<wg>-issue-<repo-slug>-<NNN>.md

Inside:
  - YAML-style frontmatter: state, opened by, labels, participants
  - The issue body as the first message
  - One `### [N] DATE — Author` section per comment

Canonical names are used throughout (passed via the Registry), so
"mnot" / "Mark Nottingham via Datatracker" / DMARC-rewritten variants
all render as "Mark Nottingham" — same as in threads.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from ..people import Registry
from ..utils import LogLevel, Verbosity, log


# Lightweight HTML→Markdown normalisation for issue bodies and comments.
# GitHub renders HTML inline (especially in tables, where Markdown lists
# don't work), so authors paste `<ul><li>...</li></ul>` and similar. The
# raw HTML ends up in our chunk text and snippets, hurts the list-aware
# snippet detector, and makes the corpus harder to skim. We don't need a
# full HTML parser — just the four patterns we actually see.
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_LI_RE = re.compile(r"<li[^>]*>\s*", re.IGNORECASE)
_HTML_LI_CLOSE_RE = re.compile(r"\s*</li>", re.IGNORECASE)
_HTML_LIST_TAG_RE = re.compile(r"</?(?:ul|ol)[^>]*>", re.IGNORECASE)


def _normalise_html(text: str) -> str:
    """Convert the small set of HTML constructs we routinely see in
    GitHub issue bodies into Markdown-ish equivalents.

    Handles: `<br>` / `<br/>` → newline; `<li>foo</li>` → `- foo`;
    surrounding `<ul>` / `<ol>` → stripped. Anything else passes through
    unchanged — better to leave a tag visible than to misrender a
    construct we haven't tested.
    """
    if not text or "<" not in text:
        return text
    out = _HTML_BR_RE.sub("\n", text)
    out = _HTML_LI_RE.sub("\n- ", out)
    out = _HTML_LI_CLOSE_RE.sub("", out)
    out = _HTML_LIST_TAG_RE.sub("", out)
    return out


def _canon_github(registry: Optional[Registry], login: str) -> str:
    if not login:
        return "(unknown)"
    if registry is None:
        return login
    return registry.canonical_for_github(login) or login


def _canon_with_role(registry: Optional[Registry], login: str) -> str:
    """Canonical name plus a short role tag if the registry knows one.

    Used for section headers and outline bullets where role attribution
    helps a reader weight the argument ("Editor" wrote the words being
    argued about; "Chair" rules on resolution; "Author" of a draft has
    invested in its design). The single-tag rule keeps headers compact
    — full role lists live in the people digest.
    """
    name = _canon_github(registry, login)
    if registry is None or name == "(unknown)":
        return name
    tag = registry.role_tag(name)
    return f"{name} ({tag})" if tag else name


# Phrases people use to call out an issue as a duplicate. Anchored to
# `#N` (or just `N`) so we don't match unrelated mentions of the word.
# Case-insensitive. The presence of the marker is what we surface; we
# don't try to disambiguate "this is a duplicate" vs "is this a
# duplicate?" — both signal the connection is worth knowing about.
#
# Permissive about what sits between "duplicate" / "dupe" / "dup" and
# the number: the original cited phrasing from a real comment was
# "duplicate of: #155" (note the colon). We tolerate any run of
# colon-or-space characters there, and the "of" is optional.
_DUPLICATE_RE = re.compile(
    r"\b(?:duplicate|dupe|dup)(?:\s+of)?[:\s]+#?(\d+)\b",
    re.IGNORECASE,
)


def _detect_duplicate_of(issue: Dict[str, Any]) -> Optional[int]:
    """Scan the issue body + every comment for a `duplicate of #N`
    marker. Returns the first referenced issue number, or None.

    Self-references are dropped (an issue is never a duplicate of
    itself). The check runs regardless of issue state: someone calling
    out a duplicate is informative even while the issue is open.
    """
    own_number = issue.get("number")
    candidates: List[str] = []
    body = issue.get("body") or ""
    if body:
        candidates.append(str(body))
    for comment in issue.get("comments") or []:
        text = comment.get("body") or ""
        if text:
            candidates.append(str(text))
    for text in candidates:
        match = _DUPLICATE_RE.search(text)
        if not match:
            continue
        try:
            referenced = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if referenced == own_number:
            continue
        return referenced
    return None


def _closing_rationale(
    issue: Dict[str, Any], registry: Optional[Registry]
) -> Optional[str]:
    """For closed issues, return the last comment's content as the
    closing rationale, formatted as a markdown blockquote.

    Returns None if the issue is open or has no comments. Heuristic:
    the last comment is usually either (a) the chair's resolution or
    (b) the participant's "OK closing" — both are useful context for
    a consuming LLM asking "what did the WG decide". We don't try to
    detect chair authorship explicitly because role tags already
    surface that visibly.
    """
    state = (issue.get("state") or "").upper()
    if state != "CLOSED":
        return None
    comments = issue.get("comments") or []
    if not comments:
        return None
    last = comments[-1]
    body = (last.get("body") or "").strip()
    if not body:
        return None
    when = _format_iso_to_minute(last.get("createdAt"))
    author = _canon_with_role(registry, last.get("author") or "")
    # Truncate aggressively — the rationale is metadata, not the
    # primary content. A consuming LLM that wants the full comment
    # reads the file (the section is still there in full).
    snippet = body if len(body) <= 400 else body[:397] + "..."
    quoted = "\n".join(f"> {line}" for line in snippet.splitlines())
    return f"_by {author} on {when}:_\n\n{quoted}"


def _format_iso_to_minute(value: Any) -> str:
    """Render an ISO timestamp to 'YYYY-MM-DD HH:MM'; pass through if unparseable."""
    if not isinstance(value, str):
        return ""
    # The archive uses "2026-04-19T00:00:00Z" — strip seconds + suffix.
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", value)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return value


def _participants(
    issue: Dict[str, Any], registry: Optional[Registry]
) -> List[str]:
    """Canonical names of every author + commenter on the issue."""
    seen: List[str] = []
    seen_set: set[str] = set()
    for login in [issue.get("author")] + [
        c.get("author") for c in (issue.get("comments") or [])
    ]:
        if not login:
            continue
        name = _canon_github(registry, login)
        if name in seen_set:
            continue
        seen_set.add(name)
        seen.append(name)
    return seen


def _render_issue(
    repo: str, issue: Dict[str, Any], registry: Optional[Registry]
) -> str:
    number = issue.get("number", "?")
    title = issue.get("title") or "(no title)"
    state = (issue.get("state") or "?").upper()
    author_name = _canon_with_role(registry, issue.get("author") or "")
    opened = _format_iso_to_minute(issue.get("createdAt"))
    updated = _format_iso_to_minute(issue.get("updatedAt"))
    labels = ", ".join(issue.get("labels") or [])
    comments = issue.get("comments") or []
    participants = _participants(issue, registry)

    out: List[str] = []
    out.append(f"# Issue #{number}: {title}\n")
    out.append(f"**Repository:** {repo}  ")
    # GitHub URL is reconstructible from repo + number; we emit it
    # verbatim so a citing consumer (LLM or human) doesn't have to.
    if "/" in repo and number != "?":
        out.append(f"**URL:** https://github.com/{repo}/issues/{number}  ")
    out.append(f"**State:** {state}  ")
    out.append(f"**Opened by:** {author_name} on {opened}  ")
    if updated and updated != opened:
        out.append(f"**Last updated:** {updated}  ")
    if labels:
        out.append(f"**Labels:** {labels}  ")
    out.append(
        f"**Comments:** {len(comments)}  ·  "
        f"**Participants ({len(participants)}):** "
        + ", ".join(participants)
    )
    duplicate_of = _detect_duplicate_of(issue)
    if duplicate_of is not None:
        out.append(f"**Duplicate of:** #{duplicate_of}")
    rationale = _closing_rationale(issue, registry)
    if rationale:
        out.append("")
        out.append("**Closing rationale:**\n")
        out.append(rationale)
    out.append("")

    if comments:
        out.append("## Outline\n")
        out.append(f"- **[1]** {opened} — {author_name} _(opened issue)_")
        for idx, comment in enumerate(comments, 2):
            c_when = _format_iso_to_minute(comment.get("createdAt"))
            c_author = _canon_with_role(registry, comment.get("author") or "")
            out.append(f"- **[{idx}]** {c_when} — {c_author}")
        out.append("")

    out.append("## Description\n")
    out.append(f"### [1] {opened} — {author_name} _(opened issue)_\n")
    body = _normalise_html((issue.get("body") or "").strip())
    out.append(body or "_(no description provided)_")
    out.append("")

    if comments:
        out.append("## Comments\n")
        for idx, comment in enumerate(comments, 2):
            c_when = _format_iso_to_minute(comment.get("createdAt"))
            c_author = _canon_with_role(registry, comment.get("author") or "")
            c_body = _normalise_html((comment.get("body") or "").strip())
            out.append(f"### [{idx}] {c_when} — {c_author}\n")
            out.append(c_body or "_(empty comment)_")
            out.append("")
    return "\n".join(out) + "\n"


def issue_slug(repo: str, number: Any) -> str:
    """Filename stem for an issue: `<repo-slug>-<NNN>` with repo
    slug matching the github JSON archive convention."""
    repo_slug = repo.replace("/", "-").lower()
    return f"{repo_slug}-{number}"


def write_issue_files(
    wg: str,
    cache_dir: str,
    registry: Optional[Registry] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """For each cached `<wg>-github-<repo>.json`, write per-issue .md files.

    Files are named `<wg>-issue-<repo>-<NNN>.md`. Pre-existing files
    matching the prefix are cleared before writing, so a re-gather
    cleanly reflects the current archive (no stale issues lying around).
    """
    if not os.path.isdir(cache_dir):
        return []

    # Wipe any stale per-issue files for this WG.
    prefix = f"{wg}-issue-"
    for name in os.listdir(cache_dir):
        if name.startswith(prefix) and name.endswith(".md"):
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass

    written: List[str] = []
    for name in sorted(os.listdir(cache_dir)):
        if not (name.startswith(f"{wg}-github-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as err:
            log(
                f"Skipping {name}: {type(err).__name__}: {err}",
                verbose,
                level=LogLevel.ERROR,
            )
            continue

        repo = data.get("repo", "")
        for issue in data.get("issues") or []:
            number = issue.get("number")
            if number is None:
                continue
            path = os.path.join(
                cache_dir, f"{wg}-issue-{issue_slug(repo, number)}.md"
            )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(_render_issue(repo, issue, registry))
            written.append(path)

    if written:
        log(
            f"Wrote {len(written)} per-issue files",
            verbose,
            level=LogLevel.STATUS,
        )
    return written
