"""
Generate digest / index files for a Working Group's gathered corpus.

These files give Claude (or any reader) a low-context overview of what's in the
gathered corpus so that they can navigate to specific files without having to
read the whole tree. Three files are produced:

  {wg}-_index.md    -- landing page: what's here, file inventory, usage hints
  {wg}-_issues.md   -- one row per GitHub issue (state, title, labels, etc.)
  {wg}-_threads.md  -- one row per mailing list thread (subject, n_msgs, span)

Digests are built deterministically from structured data already present in
the cache (GitHub JSON, .eml files, filenames). If an `llm` model is supplied,
one-line summaries are added inline for issues, threads, and drafts. See the
`llm` package (https://llm.datasette.io/) for model configuration.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .utils import LogLevel, Verbosity, get_cache_dir, get_wg_title, log

# --- Subject normalization for thread grouping --------------------------------

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd|fw|aw|sv)\s*:\s*|\[[^\]]+\]\s*)+",
    re.IGNORECASE,
)


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/[wg]-style prefixes for thread grouping."""
    prev = None
    cur = subject.strip()
    # Repeatedly strip until stable (handles "Re: [wg] Re: ...")
    while prev != cur:
        prev = cur
        cur = _SUBJECT_PREFIX_RE.sub("", cur).strip()
    return cur or subject.strip()


def _parse_date(date_header: Optional[str]) -> Optional[datetime]:
    if not date_header:
        return None
    try:
        return email.utils.parsedate_to_datetime(str(date_header))
    except (ValueError, TypeError, IndexError):
        return None


def _short_addr(from_header: str) -> str:
    """Reduce a From header to a display name or local-part."""
    name, addr = email.utils.parseaddr(from_header)
    if name:
        return name.strip().strip('"')
    if addr and "@" in addr:
        return addr.split("@", 1)[0]
    return from_header.strip() or "(unknown)"


# --- Optional LLM summarization ----------------------------------------------


class _Summarizer:
    """Wraps the optional `llm` package. No-op if model is None or llm missing."""

    def __init__(self, model_name: Optional[str], verbose: Verbosity):
        self.model = None
        self.verbose = verbose
        if not model_name:
            return
        try:
            import llm  # pylint: disable=import-outside-toplevel,import-error
        except ImportError:
            log(
                "Summarization requested but `llm` package is missing — "
                "this should ship with ietf-llm. Try reinstalling: "
                "pipx install --force ietf-llm",
                verbose,
                level=LogLevel.ERROR,
            )
            return
        try:
            self.model = llm.get_model(model_name)
        except Exception as err:  # pylint: disable=broad-except
            log(
                f"Could not load llm model '{model_name}': {err}",
                verbose,
                level=LogLevel.ERROR,
            )

    def active(self) -> bool:
        return self.model is not None

    def summarize(self, prompt: str, max_chars: int = 8000) -> str:
        """Return a one-line summary, or empty string on failure."""
        if not self.model:
            return ""
        try:
            response = self.model.prompt(prompt[:max_chars])
            text = str(response.text()).strip().replace("\n", " ")
            # Strip surrounding quotes if any
            if len(text) > 2 and text[0] in "\"'" and text[-1] == text[0]:
                text = text[1:-1]
            return text
        except Exception as err:  # pylint: disable=broad-except
            log(f"LLM summary failed: {err}", self.verbose, level=LogLevel.PROGRESS)
            return ""


_ISSUE_PROMPT = (
    "Summarize this IETF working group GitHub issue in ONE sentence "
    "(max 25 words). Focus on the substantive question or proposal, not "
    "process. No preamble.\n\nTitle: {title}\n\n{body}"
)

_THREAD_PROMPT = (
    "Summarize this IETF working group mailing list thread in ONE sentence "
    "(max 25 words). Focus on what's being discussed or decided, not who said "
    "it. No preamble.\n\nSubject: {subject}\n\nFirst message:\n{body}"
)


# --- GitHub issues digest -----------------------------------------------------


def _build_issues_digest(
    cache_dir: str,
    wg: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
) -> Optional[str]:
    """Build {wg}-_issues.md from cached GitHub JSON archives."""
    gh_files = sorted(
        f for f in os.listdir(cache_dir)
        if f.startswith(f"{wg}-github-") and f.endswith(".json")
    )
    if not gh_files:
        return None

    out_path = os.path.join(cache_dir, f"{wg}-_issues.md")
    total_open = 0
    total_closed = 0

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: GitHub issues digest\n\n")
        fh.write(
            "One row per issue across all tracked repos. For full discussion, "
            f"open the matching `{wg}-github-<repo>.txt` file and search for "
            "`Issue #N:`.\n\n"
        )

        for gh_file in gh_files:
            path = os.path.join(cache_dir, gh_file)
            try:
                with open(path, "r", encoding="utf-8") as jf:
                    data = json.load(jf)
            except (json.JSONDecodeError, OSError) as err:
                log(f"Skipping {gh_file}: {err}", verbose, level=LogLevel.ERROR)
                continue

            repo = data.get("repo", gh_file)
            issues = data.get("issues", []) or []
            source_txt = gh_file.replace(".json", ".txt")

            fh.write(f"## {repo}\n\n")
            fh.write(f"_Full text: `{source_txt}` ({len(issues)} issues)_\n\n")

            if summarizer.active():
                fh.write(
                    "| # | State | Title | Labels | Comments | Updated | Summary |\n"
                    "|---|-------|-------|--------|----------|---------|---------|\n"
                )
            else:
                fh.write(
                    "| # | State | Title | Labels | Comments | Updated | Author |\n"
                    "|---|-------|-------|--------|----------|---------|--------|\n"
                )

            # Sort: open first, then by updated desc
            def sort_key(iss: Dict[str, Any]) -> Tuple[int, str]:
                state_rank = 0 if iss.get("state") == "open" else 1
                return (state_rank, iss.get("updatedAt") or iss.get("createdAt") or "")

            issues_sorted = sorted(issues, key=sort_key, reverse=False)
            # reverse=False keeps open first; within group we want newest first
            issues_sorted.sort(
                key=lambda i: i.get("updatedAt") or i.get("createdAt") or "",
                reverse=True,
            )
            issues_sorted.sort(key=lambda i: 0 if i.get("state") == "open" else 1)

            for issue in issues_sorted:
                number = issue.get("number", "?")
                title = (issue.get("title") or "(no title)").replace("|", "\\|")
                state = issue.get("state", "?")
                labels = ", ".join(issue.get("labels", []) or [])
                labels = labels.replace("|", "\\|")
                n_comments = len(issue.get("comments", []) or [])
                updated = (issue.get("updatedAt") or issue.get("createdAt") or "")[:10]
                author = (issue.get("author") or "?").replace("|", "\\|")

                if state == "open":
                    total_open += 1
                else:
                    total_closed += 1

                if summarizer.active():
                    body = (issue.get("body") or "").strip()
                    summary = summarizer.summarize(
                        _ISSUE_PROMPT.format(title=title, body=body or "(no body)")
                    ) or ""
                    summary = summary.replace("|", "\\|")
                    fh.write(
                        f"| {number} | {state} | {title} | {labels} | "
                        f"{n_comments} | {updated} | {summary} |\n"
                    )
                else:
                    fh.write(
                        f"| {number} | {state} | {title} | {labels} | "
                        f"{n_comments} | {updated} | {author} |\n"
                    )
            fh.write("\n")

        fh.write(
            f"\n_Totals: {total_open} open, {total_closed} closed_\n"
        )

    log(
        f"Wrote issues digest: {total_open} open, {total_closed} closed",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


# --- Mailing list threads digest ---------------------------------------------


def _build_threads_digest(
    wg: str,
    cache_dir: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
) -> Optional[str]:
    """Build {wg}-_threads.md by scanning the IMAP .eml cache."""
    imap_cache = os.path.join(get_cache_dir(), wg, "imap-cache")
    if not os.path.isdir(imap_cache):
        return None

    eml_files = [f for f in os.listdir(imap_cache) if f.endswith(".eml")]
    if not eml_files:
        return None

    # thread_key -> {subject, count, participants, first, last, first_body}
    threads: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "subject": "",
            "count": 0,
            "participants": set(),
            "first": None,
            "last": None,
            "first_body": "",
        }
    )

    parsed = 0
    for eml_file in eml_files:
        path = os.path.join(imap_cache, eml_file)
        try:
            with open(path, "rb") as fh:
                msg = email.message_from_binary_file(fh, policy=email.policy.default)
        except Exception:  # pylint: disable=broad-except
            continue

        subject = str(msg.get("Subject") or "(no subject)")
        key = _normalize_subject(subject).lower()
        if not key:
            continue
        date = _parse_date(msg.get("Date"))
        sender = _short_addr(str(msg.get("From") or ""))

        thread = threads[key]
        if not thread["subject"]:
            thread["subject"] = _normalize_subject(subject)
        thread["count"] += 1
        thread["participants"].add(sender)
        if date:
            if thread["first"] is None or date < thread["first"]:
                thread["first"] = date
                # Capture body of earliest known message for summarization
                if summarizer.active():
                    try:
                        from .mbox import (  # pylint: disable=import-outside-toplevel
                            clean_email_text,
                            extract_text_content,
                        )

                        thread["first_body"] = clean_email_text(
                            extract_text_content(msg)
                        )[:4000]
                    except Exception:  # pylint: disable=broad-except
                        pass
            if thread["last"] is None or date > thread["last"]:
                thread["last"] = date
        parsed += 1

    if not threads:
        return None

    # Sort threads by last activity desc; coerce to naive datetimes for the
    # sort key only so mixed aware/naive don't raise.
    sorted_threads = sorted(
        threads.values(),
        key=lambda th: (
            th["last"].replace(tzinfo=None) if th["last"] else datetime.min
        ),
        reverse=True,
    )

    out_path = os.path.join(cache_dir, f"{wg}-_threads.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: mailing list threads digest\n\n")
        fh.write(
            f"_{len(threads)} threads across {parsed} messages, grouped by "
            "normalized subject. For full text, search the per-year "
            f"`{wg}-mailing-list-YYYY.txt` files for the subject line._\n\n"
        )

        if summarizer.active():
            fh.write(
                "| Subject | Msgs | Participants | First | Last | Summary |\n"
                "|---------|------|--------------|-------|------|---------|\n"
            )
        else:
            fh.write(
                "| Subject | Msgs | Participants | First | Last | Top senders |\n"
                "|---------|------|--------------|-------|------|-------------|\n"
            )

        for thread in sorted_threads:
            subj = (thread["subject"] or "(no subject)").replace("|", "\\|")
            if len(subj) > 100:
                subj = subj[:97] + "..."
            first = thread["first"].strftime("%Y-%m-%d") if thread["first"] else "?"
            last = thread["last"].strftime("%Y-%m-%d") if thread["last"] else "?"
            participants = sorted(thread["participants"])
            n_participants = len(participants)
            top = ", ".join(participants[:3]).replace("|", "\\|")
            if n_participants > 3:
                top += f" (+{n_participants - 3})"

            if summarizer.active():
                summary = summarizer.summarize(
                    _THREAD_PROMPT.format(
                        subject=subj,
                        body=thread["first_body"] or "(no body cached)",
                    )
                ) or ""
                summary = summary.replace("|", "\\|")
                fh.write(
                    f"| {subj} | {thread['count']} | {n_participants} | "
                    f"{first} | {last} | {summary} |\n"
                )
            else:
                fh.write(
                    f"| {subj} | {thread['count']} | {n_participants} | "
                    f"{first} | {last} | {top} |\n"
                )

    log(
        f"Wrote threads digest: {len(threads)} threads from {parsed} messages",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


# --- Top-level index ----------------------------------------------------------


def _inventory(cache_dir: str, wg: str) -> Dict[str, List[str]]:
    """Group cache files by kind for the index."""
    buckets: Dict[str, List[str]] = {
        "charter": [],
        "drafts": [],
        "rfcs": [],
        "meetings": [],
        "transcripts": [],
        "mailing_list": [],
        "github": [],
        "other": [],
    }
    for name in sorted(os.listdir(cache_dir)):
        if name.startswith(f"{wg}-_"):
            # Skip digest files themselves
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".json"):
            continue  # internal
        lower = name.lower()
        if "charter" in lower:
            buckets["charter"].append(name)
        elif "transcript" in lower:
            buckets["transcripts"].append(name)
        elif "mailing-list" in lower or "mbox" in lower:
            buckets["mailing_list"].append(name)
        elif "github" in lower:
            buckets["github"].append(name)
        elif lower.startswith("rfc"):
            buckets["rfcs"].append(name)
        elif "draft-" in lower:
            buckets["drafts"].append(name)
        elif "meeting" in lower or "minutes" in lower or "agenda" in lower or "slides" in lower:
            buckets["meetings"].append(name)
        else:
            buckets["other"].append(name)
    return buckets


def _build_index(
    wg: str,
    cache_dir: str,
    has_issues_digest: bool,
    has_threads_digest: bool,
    verbose: Verbosity,
) -> str:
    """Build {wg}-_index.md as the landing page for the corpus."""
    out_path = os.path.join(cache_dir, f"{wg}-_index.md")
    buckets = _inventory(cache_dir, wg)
    title = get_wg_title(wg) or wg

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg} ({title}) — corpus index\n\n")
        fh.write(
            f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by "
            "`ietf-llm`. Start here._\n\n"
        )

        fh.write("## How to use this corpus\n\n")
        fh.write(
            "This directory contains the public record for the IETF "
            f"**{wg}** working group: charter, drafts, meeting materials, "
            "transcripts, mailing list, and GitHub issues.\n\n"
            "The corpus is large. Prefer this index and the companion "
            "digests over reading raw files end-to-end:\n\n"
        )
        if has_issues_digest:
            fh.write(f"- `{wg}-_issues.md` — every GitHub issue, one row each.\n")
        if has_threads_digest:
            fh.write(f"- `{wg}-_threads.md` — every mailing list thread, one row each.\n")
        fh.write(
            "\nThe per-year `*-mailing-list-YYYY.txt` files and the "
            "`*-github-<repo>.txt` files are the raw text. They are often "
            "many MB — grep or targeted reads only.\n\n"
        )

        sections = [
            ("Charter", "charter"),
            ("Drafts (active)", "drafts"),
            ("RFCs", "rfcs"),
            ("Meetings (minutes / slides / agendas)", "meetings"),
            ("Transcripts", "transcripts"),
            ("Mailing list (per year)", "mailing_list"),
            ("GitHub issues (full text)", "github"),
            ("Other", "other"),
        ]
        for heading, key in sections:
            files = buckets.get(key, [])
            if not files:
                continue
            fh.write(f"## {heading} ({len(files)})\n\n")
            for name in files:
                size = os.path.getsize(os.path.join(cache_dir, name))
                fh.write(f"- `{name}` ({_fmt_size(size)})\n")
            fh.write("\n")

    log(f"Wrote index: {out_path}", verbose, level=LogLevel.STATUS)
    return out_path


def _fmt_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# --- Public entry point -------------------------------------------------------


def generate_digests(
    wg: str,
    cache_dir: str,
    summarize_model: Optional[str] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Generate all digest files for the WG. Returns paths of generated files."""
    log("Generating digests...", verbose, level=LogLevel.STATUS)
    summarizer = _Summarizer(summarize_model, verbose)
    if summarize_model and not summarizer.active():
        log(
            "Continuing with deterministic digests only.",
            verbose,
            level=LogLevel.STATUS,
        )

    generated: List[str] = []

    issues_path = _build_issues_digest(cache_dir, wg, summarizer, verbose)
    if issues_path:
        generated.append(issues_path)

    threads_path = _build_threads_digest(wg, cache_dir, summarizer, verbose)
    if threads_path:
        generated.append(threads_path)

    # Index last so it can reference the others
    index_path = _build_index(
        wg,
        cache_dir,
        has_issues_digest=issues_path is not None,
        has_threads_digest=threads_path is not None,
        verbose=verbose,
    )
    generated.append(index_path)

    return generated
