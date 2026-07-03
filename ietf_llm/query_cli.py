#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
"""`ietf-llm-query <verb> ...` — read-only queries over gathered corpora.

A thin, read-only front end over the same corpus-read functions the MCP
server exposes as tools. Every verb here reads only the local cache
(`~/.cache/ietf-llm/<corpus>/`); none reach the network or write anything.
It exists so a portable Agent Skill — or a shell user — can drive the corpus
without configuring the MCP server.

Exit codes are a stable contract; callers may branch on them:

  0  success
  1  no results / empty
  2  usage error (bad arguments)
  3  corpus not present locally — gather it first with `ietf-llm <corpus>`

Later verb groups add:

  4  embedding backend unreachable (semantic-search verbs)
  5  Datatracker unreachable (live verbs)
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional, cast

from . import __version__
from .catalog import render_efforts
from .corpus_store import get_corpus_store
from .mcp_server import (
    _DIGEST_KINDS,
    tool_draft_authors,
    tool_fetch_by_url,
    tool_list_corpora,
    tool_list_files,
    tool_list_labels,
    tool_overview,
    tool_read_digest,
    tool_tally_positions,
)
from .rfcs import render_rfc, render_search
from .utils import graceful_keyboard_interrupt, maybe_autocomplete, wg_completer

EXIT_OK = 0
EXIT_EMPTY = 1
EXIT_USAGE = 2
EXIT_NO_CORPUS = 3


def _emit(text: str) -> int:
    """Print a rendered tool result to stdout and report success."""
    print(text)
    return EXIT_OK


def _require_corpus(wg: str) -> Optional[int]:
    """Return EXIT_NO_CORPUS (after a gather hint on stderr) when `wg` is not
    cached locally, else None. Read-only: never materialises a cache, so a
    mistyped name is reported rather than silently created."""
    if get_corpus_store().corpus_exists(wg):
        return None
    print(
        f"Unknown corpus {wg!r}: nothing is cached under that name. "
        f"Gather it first with `ietf-llm {wg}`, or run "
        "`ietf-llm-query list-corpora` to see what is available.",
        file=sys.stderr,
    )
    return EXIT_NO_CORPUS


def _cmd_list_corpora(args: argparse.Namespace) -> int:
    return _emit(tool_list_corpora())


def _cmd_overview(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(tool_overview(args.corpus))


def _cmd_read_digest(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(
        tool_read_digest(
            args.corpus,
            kind=args.kind,
            state=args.state,
            label=args.label,
            author=args.author,
            role=args.role,
            since=args.since,
            until=args.until,
            event_kind=args.event_kind,
            min_messages=args.min_messages,
            limit=args.limit,
            include_bodies=args.include_bodies,
            subject=args.subject,
            sort=args.sort,
            exclude_mechanical=args.exclude_mechanical,
        )
    )


def _cmd_list_labels(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(tool_list_labels(args.corpus))


def _cmd_list_files(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(tool_list_files(args.corpus, pattern=args.pattern))


def _cmd_tally_positions(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(tool_tally_positions(args.corpus, args.file))


def _cmd_fetch_by_url(args: argparse.Namespace) -> int:
    absent = _require_corpus(args.corpus)
    if absent is not None:
        return absent
    return _emit(tool_fetch_by_url(args.corpus, args.url))


def _cmd_find_efforts(args: argparse.Namespace) -> int:
    return _emit(render_efforts(args.query, limit=args.limit))


def _cmd_get_rfc(args: argparse.Namespace) -> int:
    return _emit(render_rfc(args.number))


def _cmd_rfc_search(args: argparse.Namespace) -> int:
    return _emit(
        render_search(
            args.query,
            status=args.status,
            stream=args.stream,
            level=args.level,
            wg=args.wg,
            limit=args.limit,
        )
    )


def _cmd_draft_authors(args: argparse.Namespace) -> int:
    return _emit(tool_draft_authors(args.name))


def _add_corpus_arg(sub: argparse.ArgumentParser) -> None:
    """Attach the shared corpus-first positional (with tab-completion)."""
    corpus = sub.add_argument(
        "corpus", metavar="CORPUS", help="Corpus name (e.g. 'httpbis')"
    )
    corpus.completer = wg_completer  # type: ignore[attr-defined]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ietf-llm-query",
        description="Read-only queries over gathered IETF/IRTF corpora.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser(
        "list-corpora", help="List locally gathered corpora."
    )
    p_list.set_defaults(func=_cmd_list_corpora)

    p_overview = subparsers.add_parser(
        "overview", help="Orientation for one corpus (chairs, drafts, threads)."
    )
    _add_corpus_arg(p_overview)
    p_overview.set_defaults(func=_cmd_overview)

    p_digest = subparsers.add_parser(
        "read-digest", help="Filtered catalogue from a corpus's digest files."
    )
    _add_corpus_arg(p_digest)
    p_digest.add_argument(
        "--kind",
        choices=_DIGEST_KINDS,
        default="index",
        help="Digest to read (default: index).",
    )
    p_digest.add_argument("--state", help="Filter by state (e.g. open/closed).")
    p_digest.add_argument("--label", help="Filter by label / subject prefix.")
    p_digest.add_argument("--author", help="Filter by author.")
    p_digest.add_argument("--role", help="Filter by role.")
    p_digest.add_argument("--since", metavar="YYYY-MM-DD", help="On or after.")
    p_digest.add_argument("--until", metavar="YYYY-MM-DD", help="On or before.")
    p_digest.add_argument("--event-kind", help="Timeline event kind filter.")
    p_digest.add_argument(
        "--min-messages", type=int, help="Threads with at least N messages."
    )
    p_digest.add_argument("--limit", type=int, help="Cap the number of rows.")
    p_digest.add_argument("--subject", help="Filter by subject substring.")
    p_digest.add_argument("--sort", help="Sort key (kind-specific).")
    p_digest.add_argument(
        "--exclude-mechanical",
        action="store_true",
        help="Drop mechanical/automated timeline events.",
    )
    p_digest.add_argument(
        "--include-bodies",
        action="store_true",
        help="Append issue opening bodies (issues digest only).",
    )
    p_digest.set_defaults(func=_cmd_read_digest)

    p_labels = subparsers.add_parser(
        "list-labels", help="Curation vocabulary (GitHub labels + mail prefixes)."
    )
    _add_corpus_arg(p_labels)
    p_labels.set_defaults(func=_cmd_list_labels)

    p_files = subparsers.add_parser(
        "list-files", help="Inventory of a corpus's files and chunk counts."
    )
    _add_corpus_arg(p_files)
    p_files.add_argument(
        "--pattern", help="Glob-like relpath filter (e.g. 'meetings/*')."
    )
    p_files.set_defaults(func=_cmd_list_files)

    p_tally = subparsers.add_parser(
        "tally-positions",
        help="Chair statements and (rough) position counts for one thread/issue.",
    )
    _add_corpus_arg(p_tally)
    p_tally.add_argument(
        "file", metavar="FILE", help="Thread/issue relpath (e.g. 'threads/....md')."
    )
    p_tally.set_defaults(func=_cmd_tally_positions)

    p_fetch = subparsers.add_parser(
        "fetch-by-url",
        help="Resolve a mailarchive/datatracker/github URL to cached text.",
    )
    _add_corpus_arg(p_fetch)
    p_fetch.add_argument("url", metavar="URL", help="Citation URL to resolve.")
    p_fetch.set_defaults(func=_cmd_fetch_by_url)

    p_efforts = subparsers.add_parser(
        "find-efforts", help="Rank active IETF/IRTF efforts by a topic."
    )
    p_efforts.add_argument("query", metavar="QUERY", help="Free-text topic.")
    p_efforts.add_argument(
        "--limit", type=int, default=15, help="Max efforts (default: 15)."
    )
    p_efforts.set_defaults(func=_cmd_find_efforts)

    p_getrfc = subparsers.add_parser("get-rfc", help="Full metadata for one RFC.")
    p_getrfc.add_argument("number", metavar="NUMBER", help="RFC number (e.g. 9110).")
    p_getrfc.set_defaults(func=_cmd_get_rfc)

    p_rfcsearch = subparsers.add_parser(
        "rfc-search", help="Search the published RFC series."
    )
    p_rfcsearch.add_argument("query", metavar="QUERY", help="Free-text query.")
    p_rfcsearch.add_argument("--status", help="Filter by status (e.g. current).")
    p_rfcsearch.add_argument(
        "--stream", help="Filter by stream (ietf/irtf/iab/independent)."
    )
    p_rfcsearch.add_argument("--level", help="Filter by level (e.g. std).")
    p_rfcsearch.add_argument("--wg", help="Filter by originating working group.")
    p_rfcsearch.add_argument(
        "--limit", type=int, default=50, help="Max results (default: 50)."
    )
    p_rfcsearch.set_defaults(func=_cmd_rfc_search)

    p_authors = subparsers.add_parser(
        "draft-authors", help="Authors/editors of a draft (from the cache)."
    )
    p_authors.add_argument(
        "name", metavar="DRAFT", help="Draft name (e.g. 'draft-ietf-httpbis-...')."
    )
    p_authors.set_defaults(func=_cmd_draft_authors)

    return parser


@graceful_keyboard_interrupt
def main() -> None:
    parser = build_parser()
    maybe_autocomplete(parser)
    args = parser.parse_args()
    handler = cast(Callable[[argparse.Namespace], int], args.func)
    sys.exit(handler(args))
