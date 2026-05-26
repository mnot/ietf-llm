"""Mailing list threads digest: one row per (normalised) subject.

Walks <cache>/imap-cache/<wg>/<list>/*.eml (the layout mbox.py writes),
collapses Re:/Fwd:/[wg] variants of the same subject into one thread,
and emits a markdown table sorted by most-recent-activity-first.
"""

from __future__ import annotations

import email
import email.policy
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..utils import LogLevel, Verbosity, get_cache_dir, log
from .helpers import _normalize_subject, _parse_date, _short_addr
from .summarizer import _Summarizer

_THREAD_PROMPT = (
    "Summarize this IETF working group mailing list thread in ONE sentence "
    "(max 25 words). Focus on what's being discussed or decided, not who said "
    "it. No preamble.\n\nSubject: {subject}\n\nFirst message:\n{body}"
)


def _build_threads_digest(
    wg: str,
    cache_dir: str,
    summarizer: _Summarizer,
    verbose: Verbosity,
) -> Optional[str]:
    """Build {wg}-_threads.md by scanning the IMAP .eml cache."""
    # mbox.py writes to <cache>/imap-cache/<wg>/<list_name>/<uid>.eml,
    # so we have to walk two levels deep (and tolerate the WG having
    # more than one list, though usually it's just one).
    imap_cache = os.path.join(get_cache_dir(), "imap-cache", wg)
    if not os.path.isdir(imap_cache):
        return None

    eml_paths: List[str] = []
    for dirpath, _, filenames in os.walk(imap_cache):
        for fname in filenames:
            if fname.endswith(".eml"):
                eml_paths.append(os.path.join(dirpath, fname))
    if not eml_paths:
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
    for path in eml_paths:
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
                        from ..mbox import (  # pylint: disable=import-outside-toplevel
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

    # Sort threads by last activity desc. Dates are always tz-aware
    # (see _parse_date), so direct comparison is safe.
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    sorted_threads = sorted(
        threads.values(),
        key=lambda th: th["last"] or _epoch,
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
