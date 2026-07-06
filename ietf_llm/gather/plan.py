"""Human-readable summary of an effective gather plan.

Split out of `cli/main.py` so the entry-point module stays under the
line-count cap; the logic is self-contained (it only reads attributes off
the parsed/merged argparse namespace)."""

from __future__ import annotations

import argparse
from typing import Iterable, List


def _gather_plan_summary(args: argparse.Namespace) -> str:
    """One-line summary of the effective (saved + CLI) gather config, so a
    re-run shows what it is about to do — the persisted sources and scope,
    not just the corpus name."""

    def _brief(items: "Iterable[str]", limit: int = 3) -> str:
        items = list(items)
        if len(items) <= limit:
            return ", ".join(items)
        return ", ".join(items[:limit]) + f" (+{len(items) - limit} more)"

    sources = getattr(args, "_global_sources", {})

    def _src(name: str) -> str:
        # Flag where a global setting was resolved from, so a surprising
        # value (e.g. embed=off nobody asked for this run) is traceable.
        # CLI / default are unremarkable — only annotate env and config.
        src = sources.get(name)
        return f" (from {src})" if src in ("env", "config") else ""

    parts: List[str] = [f"months={args.months}"]
    if args.new_drafts:
        parts.append("new-drafts (rolling window)")
    if args.author:
        parts.append(f"author={args.author}")
    if args.draft:
        parts.append(f"drafts: {_brief(args.draft)}")
    if args.mailing_list:
        parts.append(f"lists: {_brief(args.mailing_list)}")
    if args.github:
        parts.append(f"github: {_brief(args.github)}")
    if args.add_mentioned_drafts:
        parts.append("add-mentioned-drafts")
    if args.include_related_drafts:
        parts.append("include-related-drafts")
    if args.github_label:
        parts.append(f"labels: {_brief(args.github_label)}")
    if args.exclude_github_label:
        parts.append(f"exclude-labels: {_brief(args.exclude_github_label)}")
    parts.append(("embed=off" if args.no_embed else "embed=on") + _src("no_embed"))
    if args.summarize:
        parts.append("summarize" + _src("summarize"))
    return " · ".join(parts)
