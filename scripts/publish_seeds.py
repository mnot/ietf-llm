#!/usr/bin/env python
"""Publish curated corpora to a static seed store (issue #182).

A one-shot operator tool: for each member of the store it incremental-gathers
(the normal `ietf-llm` CLI), then bundles and publishes what changed, and
rebuilds `index.json`. Membership lives in the store itself (`--add` / `--remove`),
so a bare run refreshes everything already in it. Run it with a Python that has
the embedding model installed (your normal ietf-llm environment) and pointed at
your populated `~/.cache/ietf-llm`:

    # bootstrap the set once
    .venv/bin/python scripts/publish_seeds.py ~/seed-store --add httpbis --add tls

    # the whole monthly job (gather each member, publish, prune), then sync
    .venv/bin/python scripts/publish_seeds.py ~/seed-store --prune
    rsync -a ~/seed-store/ host:/var/www/seed/

The script only manages a local directory; pushing it to the static host
(rsync / aws s3 sync / wrangler r2) is out of scope. See docs/seed-store.md.

Not a console entry point — producer-only, kept out of the installed CLI surface.
The publish logic lives in `ietf_llm.seed.publish`; this is the CLI shell.
"""

from __future__ import annotations

import argparse
import sys

from ietf_llm.seed.publish import (
    PublishError,
    PublishReport,
    generation_dir,
    publish_store,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("store", help="the seed-store directory to build/refresh")
    parser.add_argument(
        "corpora", nargs="*",
        help="process only these members this run (default: all members)",
    )
    parser.add_argument(
        "--add", action="append", metavar="CORPUS", default=[],
        help="add a corpus to the store (repeatable)",
    )
    parser.add_argument(
        "--remove", action="append", metavar="CORPUS", default=[],
        help="drop a corpus from membership (repeatable)",
    )
    parser.add_argument(
        "--months", type=int, default=None,
        help="window recorded for corpora being added (default: 12)",
    )
    parser.add_argument(
        "--no-gather", action="store_true",
        help="publish the current cache contents without re-gathering first",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-bundle members already published at their current version",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="delete store dirs for corpora no longer members",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the gather/publish/skip/prune plan; write nothing",
    )
    return parser


def _print_report(report: PublishReport, dry_run: bool) -> None:
    verb = "would " if dry_run else ""
    for name in report.added:
        print(f"  added    {name}")
    for name in report.removed:
        print(f"  removed  {name}")
    for name, version, size in report.published:
        mb = f"{size / 1e6:.1f} MB" if size else ""
        print(f"  {verb}publish {name:<16} {version}   {mb}".rstrip())
    for name in report.uptodate:
        print(f"  up to date {name}")
    for name in report.pruned:
        print(f"  {verb}prune   {name}")
    for name, reason in report.skipped:
        print(f"  skip     {name}: {reason}")
    n_pub = len(report.published)
    print(
        f"{'(dry run) ' if dry_run else ''}"
        f"{n_pub} published, {len(report.uptodate)} up to date, "
        f"{len(report.skipped)} skipped."
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # Published under a per-generation subdirectory so a schema bump does not
    # strand clients: see `seed.publish.generation_dir`.
    store = generation_dir(args.store)
    if store != args.store:
        print(f"publishing into {store}", file=sys.stderr)
    try:
        report = publish_store(
            store,
            process=args.corpora or None,
            add=args.add or None,
            remove=args.remove or None,
            months=args.months,
            no_gather=args.no_gather,
            force=args.force,
            prune=args.prune,
            dry_run=args.dry_run,
        )
    except PublishError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # The gather subprocess already printed its own "Interrupted." and was
        # reaped by subprocess.run; without this the parent would dump a raw
        # traceback on Ctrl-C. Newline first because Ctrl-C lands mid-line.
        print("\nInterrupted.", file=sys.stderr)
        return 130
    _print_report(report, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
