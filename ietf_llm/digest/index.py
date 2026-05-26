"""Top-level corpus index: {wg}-_index.md.

Categorises every file in the cache by kind (charter, drafts, RFCs,
meetings, transcripts, mailing list, GitHub, other) and writes a
markdown landing page that points readers at the digests and the
raw files alike.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List

from ..utils import LogLevel, Verbosity, get_wg_title, log
from .helpers import _fmt_size


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
