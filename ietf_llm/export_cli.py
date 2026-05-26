"""`ietf-llm-export` — emit a gathered WG cache as a NotebookLM-ready sink.

Two output modes, mutually exclusive:

  ietf-llm-export <wg> --destination <dir>
      Mirror the cache to a local directory.

  ietf-llm-export <wg> --create <GCP_PROJECT_ID>
      Create a new notebook in NotebookLM Enterprise and upload the cache
      as sources. Requires --credentials-file (OAuth2 client secrets) and
      writes a token to --token-file on first run.

Per project policy, every export is complete and fresh — there is no
incremental / delta mode. To get a fresh NotebookLM with current state,
re-run this and create a new notebook (delete the old one in the UI).

Per-WG flags are persisted in ~/.config/ietf-llm/<wg>/export.json so you
don't have to repeat them. Use `ietf-llm <wg> --clear-config` to reset.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, config, export
from .freshness import staleness_warning
from .utils import Verbosity, get_config_dir

SCOPE = "export"


def main() -> None:
    default_credentials = os.path.join(get_config_dir(), "client_secrets.json")
    default_token = os.path.join(get_config_dir(), "token.json")

    parser = argparse.ArgumentParser(
        description=(
            "Export a gathered IETF WG cache to a local directory "
            "(for hand-upload to NotebookLM) or to NotebookLM Enterprise."
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("wg", help="Working Group short name (e.g. 'httpbis')")
    sink = parser.add_mutually_exclusive_group()
    sink.add_argument(
        "--destination",
        metavar="DIR",
        help="Mirror the WG cache to this directory as .txt/.md files.",
    )
    sink.add_argument(
        "--create",
        metavar="GCP_PROJECT_ID",
        help="Create a new NotebookLM notebook on this GCP project and "
        "upload the cache as sources.",
    )
    parser.add_argument(
        "--credentials-file",
        default=default_credentials,
        help="OAuth2 client secrets JSON (for --create). "
        f"Default: {default_credentials}",
    )
    parser.add_argument(
        "--token-file",
        default=default_token,
        help="Where to cache the OAuth2 token (for --create). "
        f"Default: {default_token}",
    )
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    config.merge(
        args,
        wg=args.wg,
        scope=SCOPE,
        scalars=("destination", "create", "credentials_file", "token_file"),
        lists=(),
        defaults={
            "credentials_file": default_credentials,
            "token_file": default_token,
        },
    )

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    if not args.destination and not args.create:
        print(
            "Error: either --destination DIR or --create GCP_PROJECT_ID is "
            "required (or persisted from a previous run).",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.destination and args.create:
        # Both got resolved from CLI + config; prefer CLI-explicit. Since
        # we can't tell what was CLI vs persisted at this point, refuse.
        print(
            "Error: both --destination and --create are set (one may be "
            "persisted from a previous run). Run "
            f"`ietf-llm {args.wg} --clear-config` to reset, then choose one.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Warn before exporting — the export itself is a no-op cache mirror,
    # so stale-in stale-out. We want this to land in front of the user
    # before they spend bytes / GCP quota on stale material.
    warning = staleness_warning(args.wg)
    if warning:
        print(warning, file=sys.stderr)

    if args.destination:
        export.directory(args.wg, args.destination, verbose=verbosity)
    else:
        export.notebooklm(
            args.wg,
            args.create,
            args.credentials_file,
            args.token_file,
            verbose=verbosity,
        )
