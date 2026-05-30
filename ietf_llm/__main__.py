#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
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
from typing import Any, Dict, List, Optional

from . import __version__, config, corpus, paths
from .digest import generate_digests
from .digest.timeline import write_timeline_digest
from .embeddings import DEFAULT_EMBED_MODEL, build_index
from .freshness import last_gathered, record_gather
from .gather.charter import process_charter
from .gather.group_info import write_group_info
from .gather.citations import (
    citation_counts,
    scan_citations,
    write_citations_digest,
)
from .gather.drafts import (
    process_documents,
    process_extra_drafts,
    validate_draft_names,
)
from .gather.github import download_github_issues, process_github_issues
from .gather.issue_files import write_issue_files
from .gather.mail_threads import write_thread_files
from .gather.mbox import sync_mailing_list, validate_list_names
from .gather.meetings import process_meetings
from .gather.pdf_extract import extract_all_pdfs
from .gather.recent_drafts import fetch_new_draft_names, prune_drafts
from .gather.transcript_context import enrich_transcripts
from .gather.transcripts import process_transcripts
from .people import build_registry, write_people_digest
from .skill_install import install
from .utils import (
    DEFAULT_MONTHS,
    LogLevel,
    Verbosity,
    cached_wg_names,
    fetch_group_object,
    get_cache_dir,
    get_wg_file_cache_dir,
    graceful_keyboard_interrupt,
    is_synthetic_wg,
    log,
    maybe_autocomplete,
    wg_completer,
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
    used = [
        a for a in argv if any(a == f or a.startswith(f + "=") for f in _MOVED_FLAGS)
    ]
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
    wg_arg = parser.add_argument(
        "wg",
        nargs="?",
        help="IETF Working Group short name (e.g. 'httpbis'). "
        "Optional when using --install-claude-skill or --all.",
    )
    # Tab-completes already-gathered WG shortnames from the cache.
    wg_arg.completer = wg_completer  # type: ignore[attr-defined]
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_wgs",
        help="List the working groups already cached under "
        "~/.cache/ietf-llm/ (with last-gathered date), then exit. "
        "Does not gather.",
    )
    parser.add_argument(
        "--completion",
        choices=("bash", "zsh", "fish"),
        metavar="SHELL",
        help="Print a shell tab-completion script for all ietf-llm "
        "commands (bash | zsh | fish), then exit. Enable with "
        'e.g. `eval "$(ietf-llm --completion zsh)"` in your shell rc. '
        "Completes cached WG shortnames for the `wg` argument.",
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
        "--new-drafts",
        action="store_true",
        dest="new_drafts",
        help="Make this a 'new Internet-Drafts' subscription: gather every "
        "draft whose -00 was submitted within the --months window (the "
        "positional name is just a label). A rolling window — drafts that "
        "age out are pruned on re-gather. Persisted.",
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
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Only output errors."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Detailed progress reporting."
    )

    maybe_autocomplete(parser)
    args = parser.parse_args()

    if args.completion:
        sys.exit(_print_completion(args.completion))

    if args.install_claude_skill:
        sys.exit(install())

    if args.list_wgs:
        sys.exit(_print_cached_wgs())

    if args.all and args.wg:
        parser.error("--all is mutually exclusive with a positional wg argument")
    if args.all and args.clear_config:
        parser.error(
            "--clear-config is refused with --all (too easy to nuke "
            "every WG's config by accident); clear one WG at a time"
        )
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
    """Acronyms of every WG with a files/ subdir in the cache.

    Thin alias for `utils.cached_wg_names()` — kept as a local name
    because `--all` and `--list` read naturally with it.
    """
    return cached_wg_names()


def _print_completion(shell: str) -> int:
    """Print the argcomplete registration snippet for every ietf-llm
    command, for the given shell. Returns an exit code.

    Routed through `ietf-llm` itself (not argcomplete's own
    `register-python-argcomplete` script) because under `pipx` only
    this package's declared entry points are on PATH — a dependency's
    scripts aren't exposed. `eval "$(ietf-llm --completion zsh)"`
    works regardless of how the package was installed.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        print(
            "argcomplete is not installed (it ships with ietf-llm; "
            "try reinstalling).",
            file=sys.stderr,
        )
        return 1
    commands = ["ietf-llm", "ietf-llm-export", "ietf-llm-search"]
    # argcomplete ships no type stubs; shellcode isn't in its __all__.
    snippet = argcomplete.shellcode(  # type: ignore[attr-defined,no-untyped-call]
        commands,
        shell=shell,
    )
    print(snippet)
    return 0


def _print_cached_wgs() -> int:
    """Print the cached corpora — name, kind, status, last-gathered —
    to stdout. Returns 0 if any were found, 1 if the cache is empty.
    """
    wgs = _discover_gathered_wgs()
    if not wgs:
        print(
            "No corpora cached yet. Run `ietf-llm <name>` "
            "(e.g. `ietf-llm httpbis`) to gather one.",
            file=sys.stderr,
        )
        return 1
    rows = []
    for wg in wgs:
        kind, status = corpus.kind_status(wg)
        when = last_gathered(wg)
        date_str = when.strftime("%Y-%m-%d") if when is not None else "unknown"
        rows.append((wg, kind, status or "—", date_str))
    name_w = max(len(r[0]) for r in rows + [("corpus",)])
    kind_w = max(len(r[1]) for r in rows + [("", "kind")])
    status_w = max(len(r[2]) for r in rows + [("", "", "status")])
    header = (
        f"{'corpus'.ljust(name_w)}  {'kind'.ljust(kind_w)}  "
        f"{'status'.ljust(status_w)}  last gathered"
    )
    print(header)
    print("-" * len(header))
    for name, kind, status, date_str in rows:
        print(
            f"{name.ljust(name_w)}  {kind.ljust(kind_w)}  "
            f"{status.ljust(status_w)}  {date_str}"
        )
    return 0


def _resolve_corpus_shape(
    args: argparse.Namespace,
    persisted: Dict[str, Any],
    verbosity: Verbosity,
) -> "Optional[tuple[bool, bool]]":
    """Classify the corpus and return `(synth, group_backed)`, or None
    if the name is unusable (logged).

    A name backed by a Datatracker group (WG / RG / edwg / BoF) is
    `group_backed` and gets the full auto-sourced pipeline. Synthetic
    `x-` corpora and any other name are "custom": content comes only
    from explicit --mailing-list / --draft / --github. As a
    convenience, a custom name with no sources is treated as a mailing
    list (so `ietf-llm last-call` Just Works); a name that is neither a
    group, a known list, nor configured with sources is almost
    certainly a typo'd WG name, so we reject it rather than silently
    produce an empty corpus.
    """
    if is_synthetic_wg(args.wg):
        return (True, False)

    # Generative-source flags (--new-drafts; later --author) make this a
    # custom subscription corpus — the name is a label, no group lookup.
    if args.new_drafts or persisted.get("new_drafts"):
        return (False, False)

    if fetch_group_object(args.wg) is not None:
        return (False, True)

    has_sources = bool(
        args.mailing_list
        or args.draft
        or args.github
        or persisted.get("mailing_list")
        or persisted.get("draft")
        or persisted.get("github")
    )
    if not has_sources:
        if validate_list_names([args.wg], verbosity):
            args.mailing_list = [args.wg]
        else:
            log(
                f"'{args.wg}' is not a Working Group / Research Group, a "
                "known mailing list, or a synthetic (x-) corpus. Check the "
                "spelling, or add sources with --mailing-list / --draft / "
                "--github.",
                verbosity,
                level=LogLevel.ERROR,
            )
            return None
    return (False, False)  # custom / list corpus


def _download_github_archives(
    repos: "Optional[List[str]]", cache_dir: str, verbosity: Verbosity
) -> "List[tuple[str, str]]":
    """Download each configured repo's issue archive JSON. Returns
    `[(json_path, raw_txt_path)]` for the ones that downloaded, deferred
    so the .txt is rendered after the registry exists (canonical names).
    """
    pending: List[tuple[str, str]] = []
    if not repos:
        return pending
    os.makedirs(paths.github_dir(cache_dir), exist_ok=True)
    os.makedirs(paths.raw_dir(cache_dir), exist_ok=True)
    for repo_short in repos:
        if repo_short.startswith("http"):
            # URL form — the last two path segments are "<owner>/<repo>".
            repo_short = "/".join(repo_short.rstrip("/").split("/")[-2:])
        gh_json = paths.github_archive_path(cache_dir, repo_short)
        gh_txt = paths.raw_github_text_path(cache_dir, repo_short)
        if download_github_issues(repo_short, gh_json, verbose=verbosity):
            pending.append((gh_json, gh_txt))
    return pending


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

    persisted = config.load(args.wg, SCOPE)

    shape = _resolve_corpus_shape(args, persisted, verbosity)
    if shape is None:
        return  # unusable name (typo); _resolve_corpus_shape logged why
    synth, group_backed = shape

    # Validate the *new* CLI-provided --draft / --mailing-list values
    # against their authoritative sources before config.merge persists
    # them. Trust values already in gather.json from prior runs — they
    # passed validation once, and re-validating means a transient
    # Datatracker / mailarchive outage would trash working config.
    # Without this gate, a typo'd name sticks in gather.json and
    # logs the same skip line every subsequent run.
    if args.draft or args.mailing_list:
        persisted_drafts = set(persisted.get("draft", []))
        persisted_lists = set(persisted.get("mailing_list", []))
        if args.draft:
            new = [d for d in args.draft if d not in persisted_drafts]
            if new:
                ok = set(validate_draft_names(new, verbosity))
                args.draft = [
                    d for d in args.draft if d in persisted_drafts or d in ok
                ] or None
        if args.mailing_list:
            new = [lst for lst in args.mailing_list if lst not in persisted_lists]
            if new:
                ok = set(validate_list_names(new, verbosity))
                args.mailing_list = [
                    lst
                    for lst in args.mailing_list
                    if lst in persisted_lists or lst in ok
                ] or None

    config.merge(
        args,
        wg=args.wg,
        scope=SCOPE,
        scalars=(
            "months",
            "summarize",
            "summarize_model",
            "no_embed",
            "embed_model",
            "new_drafts",
        ),
        lists=(
            "github",
            "github_label",
            "exclude_github_label",
            "draft",
            "mailing_list",
        ),
        defaults={
            "months": DEFAULT_MONTHS,
            "summarize": False,
            "no_embed": False,
            "new_drafts": False,
        },
    )

    wg_cache_dir = os.path.join(get_cache_dir(), args.wg)
    cache_dir = get_wg_file_cache_dir(args.wg)
    if args.clear_cache:
        log(f"Clearing cache for {args.wg}...", verbosity, level=LogLevel.STATUS)
        if os.path.exists(wg_cache_dir):
            shutil.rmtree(wg_cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

    # `synth` (x-) and `group_backed` were resolved above. A corpus
    # that's neither is "custom" (list/draft/github sources only).
    if synth and not (args.draft or args.mailing_list or args.github):
        log(
            f"{args.wg}: synthetic (`x-`) corpus with no --draft / "
            "--mailing-list / --github configured. Gather will produce "
            "no content. Add sources and re-run.",
            verbosity,
            level=LogLevel.STATUS,
        )

    if verbosity != Verbosity.QUIET:
        if group_backed:
            label = "Processing WG"
        elif synth:
            label = "Processing synthetic corpus"
        else:
            label = "Processing custom corpus"
        print(f"{label}: {args.wg}", file=sys.stderr)
        print(f"Cache: {cache_dir}", file=sys.stderr)
        if args.clear_cache:
            print("Clear cache: re-downloading all materials.", file=sys.stderr)
        print("-" * 40, file=sys.stderr)

    # Charter / meetings / WG document list / transcripts — all
    # Datatracker-sourced. Skipped for corpora with no backing group
    # (synthetic and custom): there's no charter, no WG meetings,
    # no auto-discoverable document set.
    meeting_clusters: List[Any] = []
    if group_backed:
        charter_file = paths.charter_path(cache_dir)
        os.makedirs(os.path.dirname(charter_file) or cache_dir, exist_ok=True)
        process_charter(args.wg, charter_file, verbose=verbosity)
        # WG-level metadata (status / area / Additional Resources).
        write_group_info(args.wg, cache_dir, verbose=verbosity)
        # Returns the meeting clusters (date-spans → canonical codes)
        # so transcripts can match interim transcripts to the right
        # clustered meeting rather than orphaning them.
        meeting_clusters = process_meetings(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
        )

    # Mailing list. Auto-discovery (Datatracker → list name) skipped
    # for synthetic corpora; --mailing-list extras still work.
    sync_mailing_list(
        args.wg,
        cache_dir,
        months=args.months,
        extra_lists=args.mailing_list,
        auto_discover=group_backed,
        verbose=verbosity,
    )

    if group_backed:
        # Transcripts: download, then prepend a meeting-context header
        # to each so chunks deep in a 200KB transcript carry attribution.
        # Interim transcripts (no meeting number) are matched to a
        # meeting cluster by date span; only truly unmatched ones orphan.
        process_transcripts(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
            meeting_clusters=meeting_clusters,
        )
        enrich_transcripts(cache_dir, verbose=verbosity)

        # Documents (drafts & RFCs) — only auto-discoverable for real WGs.
        process_documents(args.wg, cache_dir, verbose=verbosity)
    # Extra drafts added via --draft. These aren't attributed to the WG
    # in the document API (often individual / author submissions the WG
    # is tracking but doesn't own), so they need explicit naming. For
    # synthetic corpora, this is the ONLY draft source.
    if args.draft:
        process_extra_drafts(args.draft, cache_dir, verbose=verbosity)

    # New-Internet-Drafts subscription: every -00 submitted in the
    # --months window. A rolling window — drafts that aged out are
    # pruned (explicit --draft additions are kept).
    if args.new_drafts:
        new_names = fetch_new_draft_names(args.months, verbose=verbosity)
        process_extra_drafts(new_names, cache_dir, verbose=verbosity)
        prune_drafts(
            cache_dir, new_names + (args.draft or []), verbose=verbosity
        )

    # Extract text from any PDFs in the cache (slide decks, whiteboards,
    # etc.). Writes a sibling .pdf.txt for each so the chunker picks
    # them up — slides become searchable content rather than invisible
    # binaries.
    extract_all_pdfs(cache_dir, verbose=verbosity)

    # GitHub issues — download the raw JSON archives first, but defer
    # rendering the .txt files until after the registry is built so the
    # Author / Comment-by lines can use canonical names.
    gh_pending = _download_github_archives(args.github, cache_dir, verbosity)

    # Identity registry — consolidates mail/GitHub/Datatracker/draft
    # surface forms into canonical actors. Built BEFORE the github .txt
    # files are rendered so author lines come out canonical.
    registry = build_registry(
        args.wg,
        verbose=verbosity,
        with_datatracker_roles=group_backed,
    )

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

    # Cross-link drafts to threads / issues that cite them. Scans the
    # per-thread and per-issue .md files we just wrote and emits
    # digests/citations.md plus a citation count per draft that
    # overview can surface inline. Has to run AFTER write_thread_files
    # and write_issue_files; runs BEFORE generate_digests so the
    # overview's documents section can pick up the counts.
    citations_map = scan_citations(cache_dir, verbose=verbosity)
    write_citations_digest(cache_dir, citations_map, verbose=verbosity)
    # citation_counts() is used by overview at tool-call time; nothing
    # consumes the in-memory map further in the gather pipeline. Kept
    # imported so a future caller can compute it without re-scanning.
    _ = citation_counts

    # People digest
    write_people_digest(args.wg, cache_dir, registry, verbose=verbosity)

    # Timeline digest
    write_timeline_digest(
        args.wg,
        cache_dir,
        registry,
        months=args.months,
        verbose=verbosity,
        group_backed=group_backed,
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
