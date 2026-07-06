#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
"""`ietf-llm-search <name> <query>` — query the embedding index."""

from __future__ import annotations

import argparse
import sys

from .. import __version__
from ..completion import maybe_autocomplete, wg_completer
from ..embeddings import search
from ..log import Verbosity, graceful_keyboard_interrupt


@graceful_keyboard_interrupt
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Semantic search over a gathered corpus."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    wg_arg = parser.add_argument(
        "wg", metavar="NAME", help="Corpus name (e.g. 'httpbis')"
    )
    wg_arg.completer = wg_completer  # type: ignore[attr-defined]
    parser.add_argument("query", help="Search query (natural language)")
    parser.add_argument(
        "-k", "--top", type=int, default=10, help="Number of hits (default: 10)"
    )
    parser.add_argument(
        "--format",
        choices=("text", "tsv"),
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--file",
        metavar="LIKE",
        help="Only consider chunks whose filename matches this SQL LIKE "
        "pattern (e.g. '%%mailing-list%%' or '%%github%%').",
    )
    parser.add_argument(
        "--since",
        metavar="ISO_DATE",
        help="Only consider chunks dated on or after this date "
        "(e.g. 2026-01-01). Applies to mailing-list and GitHub chunks; "
        "windowed draft chunks have no date and are excluded.",
    )
    parser.add_argument(
        "--until",
        metavar="ISO_DATE",
        help="Only consider chunks dated on or before this date.",
    )
    parser.add_argument("--quiet", "-q", action="store_true")
    maybe_autocomplete(parser)
    args = parser.parse_args()

    verbosity = Verbosity.QUIET if args.quiet else Verbosity.STATUS
    hits = search(
        args.wg,
        args.query,
        k=args.top,
        file_pattern=args.file,
        since=args.since,
        until=args.until,
        verbose=verbosity,
    )

    if not hits:
        print("(no results)", file=sys.stderr)
        sys.exit(1)

    if args.format == "tsv":
        for hit in hits:
            lines = (
                f"L{hit.start_line}-{hit.end_line}"
                if hit.start_line is not None
                else "L?"
            )
            print(
                f"{hit.score:.3f}\t{hit.file}\t{hit.chunk_idx}\t"
                f"{lines}\t{hit.title}\t{hit.snippet}"
            )
        return

    for i, hit in enumerate(hits, 1):
        lines = (
            f"L{hit.start_line}-{hit.end_line}"
            if hit.start_line is not None
            else "lines unknown (legacy index; run --embed --rebuild-embeddings)"
        )
        print(
            f"[{i}] score={hit.score:.3f}  {hit.file}  (chunk {hit.chunk_idx}, {lines})"
        )
        print(f"    {hit.title}")
        print(f"    {hit.snippet}")
        print()
