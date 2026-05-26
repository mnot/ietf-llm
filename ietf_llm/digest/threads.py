"""Mailing list threads digest: one row per reconstructed thread.

Consumes `mail_threads.build_threads()` (RFC-5322-header-aware threading
with subject-fallback) so the digest table matches the per-thread .md
files written into the same cache. Each row links to its corresponding
thread file so an LLM reader can pivot from the overview into the
conversation.
"""

from __future__ import annotations

import os
from typing import Optional

from ..gather.mail_threads import build_threads, thread_slug
from ..people import Registry
from ..utils import LogLevel, Verbosity, log
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
    registry: Optional[Registry] = None,
) -> Optional[str]:
    """Build {wg}-_threads.md from the reconstructed thread graph."""
    threads = build_threads(wg, registry=registry)
    if not threads:
        return None

    # Sort by most recent activity.
    threads_sorted = sorted(
        threads,
        key=lambda t: t.span[1].timestamp() if t.span[1] else 0,
        reverse=True,
    )

    out_path = os.path.join(cache_dir, f"{wg}-_threads.md")
    total_msgs = sum(len(t.members) for t in threads)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: mailing list threads digest\n\n")
        fh.write(
            f"_{len(threads)} threads across {total_msgs} messages, "
            "reconstructed via In-Reply-To / References headers. "
            f"Each row links to its full thread file (`{wg}-thread-*.md`); "
            "open one to read the conversation in date order with quotes "
            "elided._\n\n"
        )

        if summarizer.active():
            fh.write(
                "| Subject | Msgs | Participants | First | Last | Summary |\n"
                "|---------|------|--------------|-------|------|---------|\n"
            )
        else:
            fh.write(
                "| Subject | Msgs | Participants | First | Last | File |\n"
                "|---------|------|--------------|-------|------|------|\n"
            )

        for thread in threads_sorted:
            subj = (thread.subject or "(no subject)").replace("|", "\\|")
            if len(subj) > 100:
                subj = subj[:97] + "..."
            first, last = thread.span
            first_s = first.strftime("%Y-%m-%d") if first else "?"
            last_s = last.strftime("%Y-%m-%d") if last else "?"
            participants = thread.participants
            n_p = len(participants)

            slug = thread_slug(thread.subject, first_s if first else None)
            link = f"`{wg}-thread-{slug}.md`"

            if summarizer.active():
                body_source = thread.root.body or ""
                summary = summarizer.summarize(
                    _THREAD_PROMPT.format(
                        subject=subj,
                        body=body_source[:4000] or "(no body cached)",
                    )
                ) or ""
                summary = summary.replace("|", "\\|")
                fh.write(
                    f"| {subj} | {len(thread.members)} | {n_p} | "
                    f"{first_s} | {last_s} | {summary} |\n"
                )
            else:
                fh.write(
                    f"| {subj} | {len(thread.members)} | {n_p} | "
                    f"{first_s} | {last_s} | {link} |\n"
                )

    log(
        f"Wrote threads digest: {len(threads)} threads from {total_msgs} messages",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path
