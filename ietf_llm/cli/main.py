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
import traceback
from typing import Any, Dict, Optional

from ..gather import lifecycle
from ..gather.cli import build_parser
from ..gather.lifecycle import GatherBusy, GatherOutcome
from ..gather.sequencer import (
    _gather_one,
    _persist_seed_toggle,
    embed_settings,
    preflight,
)
from ..gather.sources.catalog import ensure_catalog_index
from ..gather.sources.groups import ensure_groups_index
from ..gather.sources.repo_discovery import print_discovery
from ..gather.sources.rfc_corpus import ensure_rfc_corpus
from ..gather.sources.rfcs import ensure_rfc_index
from ..log import LogLevel, Verbosity, graceful_keyboard_interrupt, log
from ..months import months_request_error
from ..store.corpus import LeaseLost, get_corpus_store
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
    from ..singletons import groups as group_reader
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
    chartered = group_reader.load_groups()
    if chartered:
        held = sum(1 for g in chartered if group_reader.charter_text(g["acronym"]))
        print(f"Groups:        {len(chartered):,} chartered, {held:,} charters held")
    else:
        print("Groups:        chartered-group mirror not available")


def _housekeeping(
    verbosity: Verbosity,
    forced: bool = False,
    embed_model: Optional[str] = None,
    embed: bool = True,
) -> Optional[str]:
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
    ensure_groups_index(verbosity, force=forced, embed_model=embed_model, embed=embed)
    sync_if_pristine(verbosity)
    return reason


#: The source flags a status record's `spec` carries, mirroring what
#: `GatherSpec.to_dict` records for an MCP gather (only those set).
_SPEC_FIELDS = (
    "mailing_list",
    "draft",
    "github",
    "github_label",
    "exclude_github_label",
    "author",
    "new_drafts",
    "months",
    "add_mentioned_drafts",
    "include_related_drafts",
)


def _status_spec(args: argparse.Namespace) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"corpus": args.wg}
    for key in _SPEC_FIELDS:
        value = getattr(args, key, None)
        if value:
            spec[key] = list(value) if isinstance(value, list) else value
    return spec


def _gather(args: argparse.Namespace, verbosity: Verbosity) -> Optional[GatherOutcome]:
    """Gather `args.wg` through the store lifecycle: lease and fleet slot,
    seed/hydrate, the stages, then publish — which on the cloud backend is what
    makes the result visible at all (issue #258), and on the local backend is
    a no-op. Returns the outcome, or None when another gatherer (an in-session
    MCP gather here, or another host) already holds the corpus.

    A mistyped name or a corpus that is not due is settled by `preflight`
    first, against the store's published gather time, so it takes no lease,
    slot or status record and downloads nothing."""
    store = get_corpus_store()
    pre = preflight(args, verbosity, gathered_at=store.gathered_at)
    if pre.outcome is not None:
        return pre.outcome
    try:
        return lifecycle.gather_and_publish(
            store,
            args.wg,
            lambda progress, note: _gather_one(
                args, verbosity, progress=progress, note_fn=note, pre=pre
            ),
            spec=_status_spec(args),
            verbosity=verbosity,
            fresh=args.clear_cache,
        )
    except GatherBusy as busy:
        log(
            f"{busy}; not gathering it here. Check `gather_status` or re-run "
            "once it finishes.",
            verbosity,
            level=LogLevel.STATUS,
        )
        return None
    except LeaseLost as lost:
        # Another gatherer took the corpus over mid-run (or moved the pointer),
        # so nothing was published. Like a busy corpus: not an outcome of this
        # run, and a sweep carries on to the next one.
        log(str(lost), verbosity, level=LogLevel.ERROR)
        return None


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
    if args.all and args.clear_cache:
        parser.error(
            "--clear-cache is refused with --all (it would discard every "
            "corpus's cached mail, which cannot be downloaded again once it "
            "is older than --months); clear one corpus at a time"
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
        #
        # The toggle first, so `--init --seed` re-enables seeding and then acts
        # on it. Otherwise the one remedy for the state --init most often has
        # to report — seeding turned off — is a gather, which is exactly what
        # the user of this flag does not have a corpus for.
        _persist_seed_toggle(args)
        model, embed = embed_settings(args)
        reason = _housekeeping(verbosity, forced=True, embed_model=model, embed=embed)
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
        failed = False
        for wg in targets:
            one = argparse.Namespace(**copy.deepcopy(base))
            one.wg = wg
            # A sweep refreshes what needs refreshing: a corpus another gatherer
            # is already refreshing (None) needs nothing from this run.
            try:
                if _gather(one, verbosity) is GatherOutcome.UNUSABLE:
                    failed = True
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # One corpus's publish or backend fault must not leave the rest
                # of an unattended sweep stale; the exit status still reports it.
                log(
                    f"{wg}: gather failed: {type(exc).__name__}: {exc}",
                    verbosity,
                    level=LogLevel.ERROR,
                )
                log(traceback.format_exc(), verbosity, level=LogLevel.PROGRESS)
                failed = True
    else:
        # A named gather is a request for *this* invocation to do the work, so
        # a corpus held elsewhere is a failure here, as is an unusable name.
        failed = _gather(args, verbosity) in (None, GatherOutcome.UNUSABLE)

    model, embed = embed_settings(args)
    _housekeeping(verbosity, embed_model=model, embed=embed)
    if failed:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
