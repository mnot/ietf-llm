"""`ietf-llm` — gather and index an IETF Working Group's public record.

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
import os
import shutil
import sys
from typing import Any, List

from . import __version__, config, paths
from .freshness import record_gather
from .gather.charter import process_charter
from .digest import generate_digests
from .gather.drafts import process_documents, process_extra_drafts
from .gather.github import download_github_issues, process_github_issues
from .gather.issue_files import write_issue_files
from .gather.mail_threads import write_thread_files
from .gather.mbox import sync_mailing_list
from .gather.meetings import process_meetings
from .gather.pdf_extract import extract_all_pdfs
from .people import build_registry, write_people_digest
from .digest.timeline import write_timeline_digest
from .gather.transcript_context import enrich_transcripts
from .gather.transcripts import process_transcripts
from .utils import (
    DEFAULT_MONTHS,
    LogLevel,
    Verbosity,
    get_cache_dir,
    get_wg_file_cache_dir,
    graceful_keyboard_interrupt,
    log,
)

SCOPE = "gather"

# Flags that used to live here and have moved to `ietf-llm-export`. We
# keep them suppressed in argparse so we can detect attempted use and
# print a helpful redirect, rather than the generic "unrecognized
# arguments" message.
_MOVED_FLAGS = (
    "--destination",
    "--create",
    "--credentials-file",
    "--token-file",
    "--update",
)


def _default_llm_model(verbose: Verbosity) -> str:
    """Return the user's configured default `llm` model name, or a fallback."""
    try:
        import llm  # pylint: disable=import-outside-toplevel,import-error

        return str(llm.get_default_model())  # type: ignore[no-untyped-call]
    except Exception as err:  # pylint: disable=broad-except
        # llm.get_default_model() can fail many ways depending on what
        # plugins/config the user has; this is a fallback so we don't
        # narrow.
        log(
            f"Could not resolve default llm model "
            f"({type(err).__name__}: {err}); "
            "falling back to 'claude-haiku-4-5'.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return "claude-haiku-4-5"


def _detect_moved_flags(argv: list[str]) -> None:
    """If the user passed a now-moved flag, print a redirect and exit."""
    used = [a for a in argv if any(a == f or a.startswith(f + "=") for f in _MOVED_FLAGS)]
    if not used:
        return
    print(
        "Error: these flags have moved to `ietf-llm-export` (a separate "
        "tool):\n  " + " ".join(used) + "\n\n"
        "The gather CLI now only populates the cache; exporting to a local\n"
        "directory or to NotebookLM Enterprise is a separate step:\n\n"
        "  ietf-llm <wg>                       # gather (populate cache)\n"
        "  ietf-llm-export <wg> --destination <dir>      # mirror cache to dir\n"
        "  ietf-llm-export <wg> --create <GCP_PROJECT>   # upload to NotebookLM\n",
        file=sys.stderr,
    )
    sys.exit(2)


@graceful_keyboard_interrupt
def main() -> None:  # pylint: disable=too-many-branches,too-many-statements
    _detect_moved_flags(sys.argv[1:])

    parser = argparse.ArgumentParser(
        description=(
            "Gather an IETF Working Group's public record into the local "
            "cache (~/.cache/ietf-llm/<wg>/). For export to NotebookLM or "
            "a local directory, see `ietf-llm-export`."
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "wg",
        nargs="?",
        help="IETF Working Group short name (e.g. 'httpbis'). "
        "Optional when using --install-claude-skill or --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Refresh every WG that already has a cache directory under "
        "~/.cache/ietf-llm/, using each one's persisted gather config. "
        "Mutually exclusive with a positional wg argument; --clear-config "
        "is refused in this mode.",
    )
    parser.add_argument(
        "--github",
        action="append",
        metavar="OWNER/REPO",
        help="GitHub repo whose issues should be gathered (repeat for multiple).",
    )
    parser.add_argument(
        "--draft",
        action="append",
        metavar="DRAFT-NAME",
        help="Internet-Draft to track in addition to the WG's auto-"
        "discovered documents (repeat for multiple). Version suffix "
        "is stripped — pass `draft-foo-bar` or `draft-foo-bar-07` and "
        "every revision (00..current) is gathered. Persisted; future "
        "runs without --draft pick up new revisions automatically.",
    )
    parser.add_argument(
        "--mailing-list",
        action="append",
        metavar="LIST",
        dest="mailing_list",
        help="Mailing list to sync in addition to the WG's auto-"
        "discovered list (repeat for multiple). Assumes IETF hosting "
        "(`imap.ietf.org`); accepts either `foo` or `foo@ietf.org`. "
        "Persisted; future runs without --mailing-list still sync it.",
    )
    parser.add_argument(
        "--github-label",
        action="append",
        metavar="LABEL",
        help="Include only GitHub issues with this label (repeat for multiple).",
    )
    parser.add_argument(
        "--exclude-github-label",
        action="append",
        metavar="LABEL",
        help="Exclude GitHub issues with this label (repeat for multiple).",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=DEFAULT_MONTHS,
        help=f"Months of mailing list / meeting history to fetch "
        f"(default: {DEFAULT_MONTHS}).",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Add LLM-generated one-line summaries to digest files. "
        "Requires the `llm` package (https://llm.datasette.io/).",
    )
    parser.add_argument(
        "--summarize-model",
        metavar="MODEL",
        help="Model id for --summarize (defaults to `llm`'s configured default).",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip building the semantic search index. By default the "
        "index is built on every gather (incremental — only new / changed "
        "files are re-embedded). Use this to defer index work on the "
        "first gather, then re-run without the flag later.",
    )
    parser.add_argument(
        "--embed-model",
        metavar="MODEL",
        help="Embedding model id. Defaults to a small local "
        "sentence-transformers model with no API key.",
    )
    parser.add_argument(
        "--rebuild-embeddings",
        action="store_true",
        help="Drop and re-embed everything (instead of incrementally "
        "updating). Useful after a schema migration that adds a new "
        "per-chunk column.",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the local file cache for this WG and re-download.",
    )
    parser.add_argument(
        "--clear-config",
        action="store_true",
        help="Clear the persisted configuration for this WG "
        "(both gather and export scopes).",
    )
    parser.add_argument(
        "--install-claude-skill",
        action="store_true",
        help="Install the bundled Claude skill into ~/.claude/skills/ietf-llm "
        "and exit. Always overwrites any existing skill at that path. "
        "Does not gather. (Claude Code only.)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output errors.")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Detailed progress reporting."
    )

    args = parser.parse_args()

    if args.install_claude_skill:
        from .skill_install import install  # pylint: disable=import-outside-toplevel

        sys.exit(install())

    if args.all and args.wg:
        parser.error("--all is mutually exclusive with a positional wg argument")
    if args.all and args.clear_config:
        parser.error("--clear-config is refused with --all (too easy to nuke "
                     "every WG's config by accident); clear one WG at a time")
    if not args.all and not args.wg:
        parser.error(
            "wg argument is required (unless using --install-claude-skill or --all)"
        )

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    if args.all:
        targets = _discover_gathered_wgs()
        if not targets:
            print(
                "No gathered WGs found under ~/.cache/ietf-llm/. "
                "Run `ietf-llm <wg>` once per WG first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if verbosity != Verbosity.QUIET:
            print(
                f"Refreshing {len(targets)} WG(s): {', '.join(targets)}",
                file=sys.stderr,
            )
        for wg in targets:
            args.wg = wg
            _gather_one(args, verbosity)
        return

    _gather_one(args, verbosity)


def _discover_gathered_wgs() -> List[str]:
    """Return acronyms of every WG that has a files/ subdir in the cache."""
    root = get_cache_dir()
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(root, name, "files")):
            out.append(name)
    return out


def _gather_one(args: argparse.Namespace, verbosity: Verbosity) -> None:
    """Run the full gather pipeline for a single WG.

    Loads the WG's persisted config first (so per-WG --github lists etc.
    apply), then walks the gather stages in order. Mutates args in place
    via config.merge; safe to call repeatedly with different args.wg
    values for --all.
    """
    if args.clear_config:
        if config.clear(args.wg) and not args.quiet:
            print(f"Cleared configuration for {args.wg}.", file=sys.stderr)

    config.merge(
        args,
        wg=args.wg,
        scope=SCOPE,
        scalars=("months", "summarize", "summarize_model", "no_embed", "embed_model"),
        lists=(
            "github", "github_label", "exclude_github_label",
            "draft", "mailing_list",
        ),
        defaults={"months": DEFAULT_MONTHS, "summarize": False, "no_embed": False},
    )

    wg_cache_dir = os.path.join(get_cache_dir(), args.wg)
    cache_dir = get_wg_file_cache_dir(args.wg)
    if args.clear_cache:
        log(f"Clearing cache for {args.wg}...", verbosity, level=LogLevel.STATUS)
        if os.path.exists(wg_cache_dir):
            shutil.rmtree(wg_cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

    if verbosity != Verbosity.QUIET:
        print(f"Processing WG: {args.wg}", file=sys.stderr)
        print(f"Cache: {cache_dir}", file=sys.stderr)
        if args.clear_cache:
            print("Clear cache: re-downloading all materials.", file=sys.stderr)
        print("-" * 40, file=sys.stderr)

    # Charter
    charter_file = paths.charter_path(cache_dir)
    os.makedirs(os.path.dirname(charter_file) or cache_dir, exist_ok=True)
    process_charter(args.wg, charter_file, verbose=verbosity)

    # Meetings
    process_meetings(args.wg, cache_dir, verbose=verbosity, months=args.months)

    # Mailing list (year-files for grep / NotebookLM, plus per-thread
    # reconstructions for legible reading by LLM consumers). Extra
    # lists from --mailing-list are layered in on top of the
    # auto-discovered one.
    sync_mailing_list(
        args.wg, cache_dir,
        months=args.months,
        extra_lists=args.mailing_list,
        verbose=verbosity,
    )

    # Transcripts (download, then prepend a meeting-context header to
    # each so chunks deep in a 200KB transcript carry attribution).
    process_transcripts(args.wg, cache_dir, verbose=verbosity, months=args.months)
    enrich_transcripts(cache_dir, verbose=verbosity)

    # Documents (drafts & RFCs)
    process_documents(args.wg, cache_dir, verbose=verbosity)
    # Extra drafts added via --draft. These don't appear on the WG's
    # documents page (often individual / author submissions the WG is
    # tracking but doesn't own), so they need explicit naming.
    if args.draft:
        process_extra_drafts(args.draft, cache_dir, verbose=verbosity)

    # Extract text from any PDFs in the cache (slide decks, whiteboards,
    # etc.). Writes a sibling .pdf.txt for each so the chunker picks
    # them up — slides become searchable content rather than invisible
    # binaries.
    extract_all_pdfs(cache_dir, verbose=verbosity)

    # GitHub issues — download the raw JSON archives first, but defer
    # rendering the .txt files until after the registry is built so the
    # Author / Comment-by lines can use canonical names.
    gh_pending: List[tuple[str, str]] = []
    if args.github:
        # Ensure parent dirs exist for the new layout (github/ and raw/).
        os.makedirs(paths.github_dir(cache_dir), exist_ok=True)
        os.makedirs(paths.raw_dir(cache_dir), exist_ok=True)
        for repo_short in args.github:
            if repo_short.startswith("http"):
                # URL form — the last path segment is the repo name.
                # repo_short normalises to "<owner>/<repo>" for the slug.
                repo_short = repo_short.rstrip("/").split("/")[-2:]
                repo_short = "/".join(repo_short)
            gh_json = paths.github_archive_path(cache_dir, repo_short)
            gh_txt = paths.raw_github_text_path(cache_dir, repo_short)
            if download_github_issues(repo_short, gh_json, verbose=verbosity):
                gh_pending.append((gh_json, gh_txt))

    # Identity registry — consolidates mail/GitHub/Datatracker/draft
    # surface forms into canonical actors. Built BEFORE the github .txt
    # files are rendered so author lines come out canonical.
    registry = build_registry(args.wg, verbose=verbosity)

    for gh_json, gh_txt in gh_pending:
        process_github_issues(
            gh_json,
            gh_txt,
            include_labels=args.github_label,
            exclude_labels=args.exclude_github_label,
            verbose=verbosity,
            registry=registry,
        )

    # Per-issue .md files — symmetric with per-thread mail files; gives
    # each GitHub issue a structured reading view with full comment
    # history attributed to canonical names.
    write_issue_files(args.wg, cache_dir, registry=registry, verbose=verbosity)

    # Per-thread reconstructions (depends on the registry so sender
    # names are already canonical when threads are written).
    write_thread_files(args.wg, cache_dir, registry=registry, verbose=verbosity)

    # People digest
    write_people_digest(args.wg, cache_dir, registry, verbose=verbosity)

    # Timeline digest
    write_timeline_digest(
        args.wg, cache_dir, registry,
        months=args.months, verbose=verbosity,
    )

    # Digests
    summarize_model: Any = None
    if args.summarize or args.summarize_model:
        summarize_model = args.summarize_model or _default_llm_model(verbosity)
    generate_digests(
        args.wg,
        cache_dir,
        summarize_model=summarize_model,
        verbose=verbosity,
        registry=registry,
    )

    # Embedding index. Default-on now; opt out with --no-embed for
    # the rare case (long-running first gather where the user wants
    # to defer the embed cost).
    if not args.no_embed:
        from .embeddings import (  # pylint: disable=import-outside-toplevel
            DEFAULT_EMBED_MODEL,
            build_index,
        )

        build_index(
            args.wg,
            cache_dir,
            model_name=args.embed_model or DEFAULT_EMBED_MODEL,
            rebuild=args.rebuild_embeddings,
            verbose=verbosity,
        )

    # Record successful gather so freshness checks (export warning,
    # MCP staleness banner) know when to nag. Best-effort; never fatal.
    record_gather(args.wg)

    if verbosity != Verbosity.QUIET:
        print("-" * 40, file=sys.stderr)
        print(f"Cache populated at {cache_dir}.", file=sys.stderr)
        print(
            "To export: `ietf-llm-export "
            f"{args.wg} --destination <dir>` "
            "(or --create <GCP_PROJECT>).",
            file=sys.stderr,
        )


if __name__ == "__main__":  # pragma: no cover
    main()
