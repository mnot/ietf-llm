"""`ietf-llm-search <wg> <query>` — query the embedding index."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .embeddings import search
from .utils import Verbosity


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Semantic search over a Working Group's gathered corpus."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("wg", help="Working Group short name (e.g. 'httpbis')")
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
    parser.add_argument("--quiet", "-q", action="store_true")
    args = parser.parse_args()

    verbosity = Verbosity.QUIET if args.quiet else Verbosity.STATUS
    hits = search(args.wg, args.query, k=args.top, verbose=verbosity)

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
