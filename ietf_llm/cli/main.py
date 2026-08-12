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
from typing import Optional

from ..gather.cli import build_parser
from ..gather.sequencer import _gather_one
from ..gather.sources.catalog import ensure_catalog_index
from ..gather.sources.repo_discovery import print_discovery
from ..gather.sources.rfc_corpus import ensure_rfc_corpus
from ..gather.sources.rfcs import ensure_rfc_index
from ..log import Verbosity, graceful_keyboard_interrupt
from ..months import months_request_error
from . import list as cli_list
from .completion import maybe_autocomplete, print_completion_snippet
from .skill_install import install_skills, sync_if_pristine


def _init_report(rfc_corpus_reason: Optional[str] = None) -> None:
    """Say what state `--init` left the machine in.

    The housekeeping steps log only when they *change* something, which is
    right after a gather — noise nobody asked for — and wrong for a command
    whose entire job is setting a machine up. A successful `--init` on an
    already-current machine otherwise prints nothing but unrelated skill
    warnings, and the user cannot tell it from a silent failure.

    `rfc_corpus_reason` is what `ensure_rfc_corpus` returned. A guessed hint
    here cannot do that job: seeding turned off, an empty seed URL, an
    unreachable store and a store missing the entry all end in the same
    "not installed", and only the step that declined knows which.
    """
    # pylint: disable=import-outside-toplevel
    from ..gather.sources.rfc_corpus import local_build
    from ..singletons import catalog as catalog_reader
    from ..singletons.rfcs import _load as load_rfcs

    # pylint: enable=import-outside-toplevel

    rfcs = load_rfcs()
    print(
        f"RFC metadata:  {len(rfcs.all_rfcs):,} RFCs"
        if rfcs
        else "RFC metadata:  not available"
    )
    build = local_build()
    if build:
        print(f"RFC full text: installed, build {build}")
    else:
        why = rfc_corpus_reason or "the cache may not be writable"
        print(
            "RFC full text: NOT installed — search_rfc_text and "
            "get_rfc_section will be unavailable.\n"
            f"               Because: {why}."
        )
    efforts = catalog_reader._load()  # pylint: disable=protected-access
    print(
        f"Efforts:       {len(efforts):,} in the catalog"
        if efforts
        else "Efforts:       catalog not available"
    )


def _housekeeping(verbosity: Verbosity, forced: bool = False) -> Optional[str]:
    """Refresh the mirrors and skills a gather keeps current.

    Best-effort, never blocks exit. Runs after every gather — and on its own
    for `--init`, because the automatic pull rides this path and a deployment
    that only ever reads never reaches it, leaving the RFC tools unavailable
    with nothing obvious to do about it. `forced` skips the RFC corpus's
    once-an-hour throttle, since asking explicitly is the point.

    Returns why the RFC full-text corpus was not installed, if it was not, for
    `--init` to report.
    """
    ensure_rfc_index(verbosity)
    if forced:
        reason = ensure_rfc_corpus(verbosity, interval=0.0)
    else:
        reason = ensure_rfc_corpus(verbosity)
    ensure_catalog_index(verbosity)
    sync_if_pristine(verbosity)
    return reason


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
    if not args.all and not args.wg and not args.init_machine:
        parser.error(
            "a corpus name is required (unless using --init, --install-skills "
            "or --all)"
        )

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    months_error = months_request_error(args.months, args.force)
    if months_error:
        parser.error(months_error)

    if args.init_machine:
        # After verbosity is resolved, before the gather-argument validation
        # below: --init takes no corpus, so those checks do not apply to it.
        reason = _housekeeping(verbosity, forced=True)
        if verbosity is not Verbosity.QUIET:
            _init_report(reason)
        sys.exit(0)

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

    _housekeeping(verbosity)


if __name__ == "__main__":  # pragma: no cover
    main()
