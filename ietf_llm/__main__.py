#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
"""`ietf-llm` — gather and index the public record of an IETF effort.

Populates ~/.cache/ietf-llm/<wg>/ with the charter, drafts, meeting
materials, transcripts, mailing list, and GitHub issues for a Working
Group, then (optionally) builds an embedding index and LLM-summarised
digest files. The cache is the canonical source of truth — the MCP
server, `ietf-llm-search`, and `ietf-llm-export` all read from it.

For local-folder mirroring or NotebookLM Enterprise upload, see
`ietf-llm-export`.
"""

from __future__ import annotations

import argparse
import copy
import sys

from .cli import list as cli_list
from .completion import maybe_autocomplete, print_completion_snippet
from .gather.cli import build_parser
from .gather.sequencer import _gather_one
from .gather.sources.catalog import ensure_catalog_index
from .gather.sources.repo_discovery import print_discovery
from .gather.sources.rfcs import ensure_rfc_index
from .months import months_request_error
from .skill_install import install_skills, sync_if_pristine
from .utils import Verbosity, graceful_keyboard_interrupt


@graceful_keyboard_interrupt
def main() -> None:  # pylint: disable=too-many-branches,too-many-statements
    parser = build_parser()
    maybe_autocomplete(parser)
    args = parser.parse_args()

    if args.completion:
        sys.exit(print_completion_snippet(args.completion))

    if args.install_skills:
        sys.exit(install_skills())

    if args.list_wgs:
        sys.exit(cli_list.print_cached_wgs())

    if args.discover_github:
        if not args.wg:
            parser.error("--discover-github needs a Working Group name")
        sys.exit(print_discovery(args.wg))

    if args.all and args.wg:
        parser.error("--all is mutually exclusive with a positional NAME argument")
    if args.all and args.clear_config:
        parser.error(
            "--clear-config is refused with --all (too easy to nuke "
            "every corpus's config by accident); clear one corpus at a time"
        )
    if not args.all and not args.wg:
        parser.error(
            "a corpus name is required (unless using --install-skills or --all)"
        )

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    months_error = months_request_error(args.months, args.force)
    if months_error:
        parser.error(months_error)

    if args.used_within is not None:
        if not args.all:
            parser.error("--used-within is only valid with --all")
        if args.used_within < 1:
            parser.error("--used-within DAYS must be a positive integer")

    if args.all:
        targets = cli_list.all_corpora()
        if not targets:
            print(
                "No gathered corpora found. "
                "Run `ietf-llm <name>` once per corpus first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.used_within is not None:
            targets = cli_list.filter_recently_used(targets, args.used_within)
            if not targets:
                # A successful no-op for a cron: corpora exist, none were used
                # recently, so there is nothing to refresh. Distinct from the
                # empty-store error above.
                if verbosity != Verbosity.QUIET:
                    print(
                        f"No corpora read within the last {args.used_within} "
                        "days; nothing to refresh.",
                        file=sys.stderr,
                    )
                sys.exit(0)
        if verbosity != Verbosity.QUIET:
            print(
                f"Refreshing {len(targets)} corpora: {', '.join(targets)}",
                file=sys.stderr,
            )
        # Each corpus needs its own args object: _gather_one -> config.merge
        # mutates args in place (folding the corpus's persisted sources back
        # onto it) and persists the result. A shared Namespace would carry
        # corpus A's github/draft/mailing-list sources and flags into corpus
        # B's merge — union'ing them into B's saved config and compounding on
        # each --all run — and would also suppress B's repo auto-discovery.
        base = vars(args)
        for wg in targets:
            one = argparse.Namespace(**copy.deepcopy(base))
            one.wg = wg
            _gather_one(one, verbosity)
    else:
        _gather_one(args, verbosity)

    # Tail housekeeping (best-effort, never blocks exit): refresh mirrors, sync skill.
    ensure_rfc_index(verbosity)
    ensure_catalog_index(verbosity)
    sync_if_pristine(verbosity)


if __name__ == "__main__":  # pragma: no cover
    main()
