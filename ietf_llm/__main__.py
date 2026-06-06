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
import os
import shutil
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import __version__, canonical, config, corpus, http_metrics, paths
from .digest import generate_digests
from .digest.timeline import write_timeline_digest
from .embeddings import DEFAULT_EMBED_MODEL, build_index
from .freshness import last_gathered, record_gather
from .gather.author import fetch_author_draft_names, resolve_person
from .gather.catalog import ensure_catalog_index
from .gather.charter import process_charter
from .gather.citations import (
    citation_counts,
    scan_citations,
    write_citations_digest,
)
from .gather.drafts import (
    normalize_draft_name,
    process_documents,
    process_extra_drafts,
    validate_draft_names,
)
from .gather.github import (
    download_github_issues,
    process_github_issues,
    validate_github_repos,
)
from .gather.group_info import write_group_info
from .gather.issue_files import write_issue_files
from .gather.mail_threads import write_thread_files
from .gather.mbox import sync_mailing_list, validate_list_names
from .gather.meetings import process_meetings
from .gather.pdf_extract import extract_all_pdfs
from .gather.recent_drafts import fetch_new_draft_names, prune_drafts
from .gather.repo_discovery import autotrack_github, print_discovery
from .gather.rfcs import ensure_rfc_index
from .gather.transcript_context import enrich_transcripts
from .gather.transcripts import process_transcripts
from .gather_plan import _gather_plan_summary
from .gather_stages import ProgressFn, StageTracker, stage_plan
from .people import build_registry, write_people_digest
from .skill_install import install, sync_if_pristine
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
    months_request_error,
    print_completion_snippet,
    resolve_months,
    wg_completer,
)

SCOPE = "gather"

# Settings that are properties of the tool / deployment, not of a corpus:
# the embedding model, whether to embed, and the summariser. As of 0.8.0
# these are resolved GLOBALLY (env > CLI > global config > default) and are
# no longer persisted per-WG. (arg_name, env_var, default)
GLOBAL_SCALARS = (
    ("embed_model", "IETF_LLM_EMBED_MODEL", None),
    ("no_embed", "IETF_LLM_NO_EMBED", False),
    ("summarize", "IETF_LLM_SUMMARIZE", False),
    ("summarize_model", "IETF_LLM_SUMMARIZE_MODEL", None),
)
_GLOBAL_KEYS = tuple(name for name, _env, _default in GLOBAL_SCALARS)


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


def build_parser() -> argparse.ArgumentParser:
    """Construct the `ietf-llm` argument parser.

    Extracted from `main()` so the programmatic entry point
    (`run_gather`, used by the MCP gather runner) can turn a CLI-style
    argv into the same fully-defaulted, validated Namespace that `main()`
    builds — one source of truth for the flag surface and its defaults.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Gather an IETF corpus — a Working Group / RG / editorial WG / "
            "BoF, a mailing list, or a custom set of drafts — into the "
            "local cache (~/.cache/ietf-llm/<name>/). For export to "
            "NotebookLM or a local directory, see `ietf-llm-export`."
        )
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    wg_arg = parser.add_argument(
        "wg",
        nargs="?",
        metavar="NAME",
        help="Corpus to gather: a Working Group / Research Group / "
        "editorial WG / BoF shortname (e.g. 'httpbis'), a mailing list "
        "(e.g. 'last-call'), or a custom label given explicit sources. "
        "Optional when using --install-claude-skill or --all.",
    )
    # Tab-completes already-gathered WG shortnames from the cache.
    wg_arg.completer = wg_completer  # type: ignore[attr-defined]
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_wgs",
        help="List the corpora already cached under ~/.cache/ietf-llm/ "
        "(with kind, status, last-gathered date, and subject), then exit. "
        "Does not gather.",
    )
    parser.add_argument(
        "--discover-github",
        action="store_true",
        dest="discover_github",
        help="Discover and print the GitHub repos worth tracking for a Working "
        "Group (Datatracker-org repos with draft sources + an active issue "
        "tracker) and the matching `--github` flags, then exit. Writes nothing.",
    )
    parser.add_argument(
        "--completion",
        choices=("bash", "zsh", "fish"),
        metavar="SHELL",
        help="Print a shell tab-completion script for all ietf-llm "
        "commands (bash | zsh | fish), then exit. Enable with "
        'e.g. `eval "$(ietf-llm --completion zsh)"` in your shell rc. '
        "Completes cached corpus names for the `NAME` argument.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Refresh every corpus that already has a cache directory under "
        "~/.cache/ietf-llm/, using each one's persisted gather config. "
        "Mutually exclusive with a positional NAME argument; --clear-config "
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
        help="Mailing list to sync, in addition to any the corpus "
        "auto-discovers (repeat for multiple). Any list archived at "
        "mailarchive.ietf.org works — IETF, IRTF, or RFC-Editor — synced "
        "from the IETF IMAP mirror. A bare name or a full address; the "
        "domain is optional and ignored (`rswg`, `rswg@rfc-editor.org`). "
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
        "--author",
        metavar="PERSON",
        help="Make this a 'follow an author' corpus: gather every "
        "Internet-Draft authored by this person (the positional name is "
        "just a label). PERSON is an email (`mnot@mnot.net`, the "
        "unambiguous recommended form), a Datatracker person id, or an "
        'exact full name (`"Mark Nottingham"` — ambiguous names are '
        "listed). Drafts only — add --mailing-list to also follow "
        "specific lists. Persisted.",
    )
    parser.add_argument(
        "--add-mentioned-drafts",
        action="store_true",
        dest="add_mentioned_drafts",
        help="After gathering, scan the corpus's threads and issues for "
        "Internet-Drafts mentioned but not already present, and add them. "
        "Re-derived over the whole corpus each gather (backfills old "
        "mentions); sticky — once added, a draft stays. Persisted.",
    )
    parser.add_argument(
        "--include-related-drafts",
        action="store_true",
        dest="include_related_drafts",
        help="Also gather active `draft-<author>-<wg>-<topic>` drafts the "
        "WG follows but hasn't adopted (matches Datatracker's documents-"
        "page 'Related Internet-Drafts' section). Off by default — can "
        "be large for popular WGs. Persisted.",
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
        f"(default: {DEFAULT_MONTHS}). 0 means all history — an unbounded, slow "
        "gather, so it requires --force.",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        default=None,
        help="Add LLM-generated one-line summaries to digest files. "
        "Requires the `llm` package (https://llm.datasette.io/).",
    )
    parser.add_argument(
        "--summarize-model",
        metavar="MODEL",
        help="Model id for --summarize (defaults to `llm`'s configured default).",
    )
    embed_group = parser.add_mutually_exclusive_group()
    embed_group.add_argument(
        "--no-embed",
        dest="no_embed",
        action="store_const",
        const=True,
        default=None,
        help="Skip building the semantic search index. By default the "
        "index is built on every gather (incremental — only new / changed "
        "files are re-embedded). Use this to defer index work on the "
        "first gather, then re-run without the flag later. This setting is "
        "remembered globally; clear it with --embed.",
    )
    embed_group.add_argument(
        "--embed",
        dest="no_embed",
        action="store_const",
        const=False,
        help="Build the semantic search index, overriding a remembered "
        "--no-embed in the global config (the default behaviour otherwise).",
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
        "--force",
        action="store_true",
        help="Re-gather even if the corpus is within the freshness window "
        "(IETF_LLM_GATHER_MIN_INTERVAL, default 6h); overrides the debounce "
        "that otherwise skips a just-gathered corpus.",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the local file cache for this corpus and re-download.",
    )
    parser.add_argument(
        "--clear-config",
        action="store_true",
        help="Clear the persisted configuration for this corpus "
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
    return parser


@graceful_keyboard_interrupt
def main() -> None:  # pylint: disable=too-many-branches,too-many-statements
    parser = build_parser()
    maybe_autocomplete(parser)
    args = parser.parse_args()

    if args.completion:
        sys.exit(print_completion_snippet(args.completion))

    if args.install_claude_skill:
        sys.exit(install())

    if args.list_wgs:
        sys.exit(_print_cached_wgs())

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
            "a corpus name is required (unless using --install-claude-skill or --all)"
        )

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    months_error = months_request_error(args.months, args.force)
    if months_error:
        parser.error(months_error)

    if args.all:
        targets = _discover_gathered_wgs()
        if not targets:
            print(
                "No gathered corpora found under ~/.cache/ietf-llm/. "
                "Run `ietf-llm <name>` once per corpus first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if verbosity != Verbosity.QUIET:
            print(
                f"Refreshing {len(targets)} corpora: {', '.join(targets)}",
                file=sys.stderr,
            )
        for wg in targets:
            args.wg = wg
            _gather_one(args, verbosity)
    else:
        _gather_one(args, verbosity)

    # Tail housekeeping (best-effort, never blocks exit): refresh mirrors, sync skill.
    ensure_rfc_index(verbosity)
    ensure_catalog_index(verbosity)
    sync_if_pristine(verbosity)


def _discover_gathered_wgs() -> List[str]:
    """Acronyms of every WG with a files/ subdir in the cache.

    Thin alias for `utils.cached_wg_names()` — kept as a local name
    because `--all` and `--list` read naturally with it.
    """
    return cached_wg_names()


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
        rows.append((wg, kind, status or "—", date_str, corpus.describe(wg)))
    name_w = max(len(r[0]) for r in rows + [("corpus",)])
    kind_w = max(len(r[1]) for r in rows + [("", "kind")])
    status_w = max(len(r[2]) for r in rows + [("", "", "status")])
    header = (
        f"{'corpus'.ljust(name_w)}  {'kind'.ljust(kind_w)}  "
        f"{'status'.ljust(status_w)}  {'last gathered'}  about"
    )
    print(header)
    print("-" * len(header))
    for name, kind, status, date_str, subject in rows:
        line = (
            f"{name.ljust(name_w)}  {kind.ljust(kind_w)}  "
            f"{status.ljust(status_w)}  {date_str}  {subject}"
        )
        print(line.rstrip())
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

    # Generative-source flags (--new-drafts, --author) make this a
    # custom subscription corpus — the name is a label, no group lookup.
    if (
        args.new_drafts
        or persisted.get("new_drafts")
        or args.author
        or persisted.get("author")
    ):
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
        if repo_short.startswith(("http://", "https://")):
            # URL form — the last two path segments are "<owner>/<repo>".
            repo_short = "/".join(repo_short.rstrip("/").split("/")[-2:])
        gh_json = paths.github_archive_path(cache_dir, repo_short)
        gh_txt = paths.raw_github_text_path(cache_dir, repo_short)
        if download_github_issues(repo_short, gh_json, verbose=verbosity):
            pending.append((gh_json, gh_txt))
    return pending


def _present_draft_names(cache_dir: str) -> "set[str]":
    """Normalised names of drafts already in the cache's drafts/ dir."""
    directory = paths.drafts_dir(cache_dir)
    names: set[str] = set()
    if os.path.isdir(directory):
        for fname in os.listdir(directory):
            if fname.startswith("draft-") and fname.endswith(".txt"):
                names.add(normalize_draft_name(fname))
    return names


def _gather_dynamic_drafts(
    args: argparse.Namespace,
    cache_dir: str,
    persisted: Dict[str, Any],
    verbosity: Verbosity,
) -> None:
    """Materialise the generative draft sources (--author, --new-drafts).

    `--author` is additive; `--new-drafts` is a rolling window — drafts
    aging out are pruned, but everything else we intend to keep
    (explicit --draft, authored drafts, and previously-added mentioned
    drafts) is retained.
    """
    author_names: List[str] = []
    if args.author:
        resolved = resolve_person(args.author, verbose=verbosity)
        if resolved is not None:
            author_names = fetch_author_draft_names(resolved[0], verbose=verbosity)
            process_extra_drafts(author_names, cache_dir, verbose=verbosity)
            _persist_author_name(args.wg, resolved[1])

    if args.new_drafts:
        new_names = fetch_new_draft_names(args.months, verbose=verbosity)
        process_extra_drafts(new_names, cache_dir, verbose=verbosity)
        keep = (
            new_names
            + (args.draft or [])
            + author_names
            + list(persisted.get("mentioned_drafts") or [])
        )
        prune_drafts(cache_dir, keep, verbose=verbosity)


def _persist_author_name(wg: str, name: str) -> None:
    """Record the resolved author's canonical name so the corpus listing
    can show *who* a follow-an-author corpus tracks, even when it was
    gathered by email or person id."""
    cfg = config.load(wg, SCOPE)
    if cfg.get("author_name") != name:
        cfg["author_name"] = name
        config.save(wg, SCOPE, cfg)


def _gather_mentioned_drafts(
    args: argparse.Namespace,
    cache_dir: str,
    mentioned: "Iterable[str]",
    persisted: Dict[str, Any],
    verbosity: Verbosity,
) -> None:
    """Add drafts mentioned in the corpus but not already present.

    `mentioned` is the set of draft names the citation scan found.
    Only newly-seen candidates are validated (to drop garbage tokens
    that would 404), then fetched. Sticky: the accumulated set is
    persisted so a draft stays once added and survives the new-drafts
    prune.
    """
    if not args.add_mentioned_drafts:
        return
    present = _present_draft_names(cache_dir)
    already = set(persisted.get("mentioned_drafts") or [])
    candidates = sorted(set(mentioned) - present - already)
    valid = set(validate_draft_names(candidates, verbosity)) if candidates else set()
    if valid:
        process_extra_drafts(sorted(valid), cache_dir, verbose=verbosity)
        log(
            f"Mentioned drafts: added {len(valid)} of {len(candidates)} "
            "new candidate(s).",
            verbosity,
            level=LogLevel.STATUS,
        )
    updated = sorted(already | valid)
    if updated != sorted(already):
        cfg = config.load(args.wg, SCOPE)
        cfg["mentioned_drafts"] = updated
        config.save(args.wg, SCOPE, cfg)


def _migrate_global_keys(
    wg: str, persisted: Dict[str, Any], verbosity: Verbosity
) -> None:
    """One-time migration for the 0.8.0 global-settings move.

    The embed / summarise settings moved from per-WG gather.json to the
    global config. Warn about any legacy per-WG values and strip them, so
    the notice does not repeat and the stale values stop shadowing the
    global ones. We do NOT auto-migrate the value (different corpora may
    disagree); the user sets it once globally instead.
    """
    moved = sorted(k for k in _GLOBAL_KEYS if k in persisted)
    if not moved:
        return
    log(
        f"Note: {', '.join(moved)} are now global settings (0.8.0); the "
        f"per-corpus values in {wg}'s gather.json are ignored and being removed. "
        f"Set them once with `ietf-llm --embed-model ...` / `--summarize` etc. "
        f"or the matching IETF_LLM_* environment variables.",
        verbosity,
        level=LogLevel.STATUS,
    )
    for key in moved:
        persisted.pop(key, None)
    config.save(wg, SCOPE, persisted)


def _validate_new_sources(
    args: argparse.Namespace,
    persisted: Dict[str, Any],
    key: str,
    validator: "Callable[[List[str], Verbosity], List[str]]",
    verbosity: Verbosity,
) -> None:
    """Drop CLI `--<key>` values that `validator` rejects, BEFORE
    `config.merge` persists them — so a typo'd name doesn't stick in
    gather.json and log the same skip line every subsequent run.

    Only *new* values are validated; ones already persisted are trusted
    (they passed once), so a transient Datatracker / mailarchive / GitHub
    outage can't trash working config.
    """
    current = getattr(args, key, None)
    if not current:
        return
    known = set(persisted.get(key, []))
    new = [v for v in current if v not in known]
    if not new:
        return
    ok = set(validator(new, verbosity))
    setattr(args, key, [v for v in current if v in known or v in ok] or None)


def run_gather(
    argv: List[str],
    verbosity: Verbosity = Verbosity.STATUS,
    progress: Optional[ProgressFn] = None,
    note_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """Programmatic gather entry point: gather one corpus from a CLI-style
    `argv` (`[corpus, "--mailing-list", "foo", ...]`).

    Parses through `build_parser()` so defaults and validation match the
    CLI exactly, then runs the pipeline. Returns True on success, False if
    the corpus name was unusable (a typo'd WG that is neither a group, a
    known list, nor configured with sources). Used by the MCP gather
    runner; not wired to any console script. `note_fn` forwards one-line
    outcome notes (e.g. auto-tracked GitHub repos) to the caller's status.
    """
    args = build_parser().parse_args(argv)
    return _gather_one(args, verbosity, progress=progress, note_fn=note_fn)


def _gather_one(  # pylint: disable=too-many-branches,too-many-statements
    args: argparse.Namespace,
    verbosity: Verbosity,
    progress: Optional[ProgressFn] = None,
    note_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    """Run the full gather pipeline for a single WG.

    Loads the WG's persisted config first (so per-WG --github lists etc.
    apply), then walks the gather stages in order. Mutates args in place
    via config.merge; safe to call repeatedly with different args.wg
    values for --all. Returns True on success, False if the corpus name
    was unusable (logged). `progress`, when given, is called as each
    stage begins (see `_stage_plan`).
    """
    http_metrics.reset()  # fresh per-corpus egress accounting; see `http_metrics`

    if args.clear_config:
        if config.clear(args.wg) and not args.quiet:
            print(f"Cleared configuration for {args.wg}.", file=sys.stderr)

    persisted = config.load(args.wg, SCOPE)
    _migrate_global_keys(args.wg, persisted, verbosity)

    shape = _resolve_corpus_shape(args, persisted, verbosity)
    if shape is None:
        return False  # unusable name (typo); _resolve_corpus_shape logged why
    synth, group_backed = shape

    # Skip an unnecessary gather (checked on raw CLI args, before merge folds
    # in persisted sources): a reuse hint when a new custom/synthetic corpus
    # duplicates an existing one, or a freshness debounce when a cached corpus
    # is refreshed too soon. --force runs anyway; a skip here is success.
    skip = canonical.cli_gather_skip(args, synthetic=synth, group_backed=group_backed)
    if skip is not None:
        log(skip, verbosity, level=LogLevel.STATUS)
        return True

    # Validate the *new* CLI-provided --draft / --mailing-list / --github
    # values against their authoritative sources before config.merge
    # persists them (see _validate_new_sources for the rationale).
    _validate_new_sources(args, persisted, "draft", validate_draft_names, verbosity)
    _validate_new_sources(
        args, persisted, "mailing_list", validate_list_names, verbosity
    )
    _validate_new_sources(args, persisted, "github", validate_github_repos, verbosity)

    # First gather, no --github: auto-track the group's discovered draft repos
    # (before config.merge folds args.github into the persisted set).
    autotrack_github(args, persisted, group_backed, SCOPE, verbosity, note_fn=note_fn)

    config.merge(
        args,
        wg=args.wg,
        scope=SCOPE,
        scalars=(
            "months",
            "new_drafts",
            "author",
            "add_mentioned_drafts",
            "include_related_drafts",
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
            "new_drafts": False,
            "include_related_drafts": False,
        },
    )
    # Embed / summarise settings are resolved globally (env > CLI > global
    # config > default), not per-WG.
    config.merge_global(args, GLOBAL_SCALARS)

    # Degrade a stored months=0 (all history) to the default on an unforced run
    # so a past forced `--months 0` does not make every refresh unbounded.
    args.months, months_note = resolve_months(args.months, args.force)
    if months_note:
        log(f"{args.wg}: {months_note}.", verbosity, level=LogLevel.STATUS)

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
        kind = (
            "WG" if group_backed else "synthetic corpus" if synth else "custom corpus"
        )
        print(f"Processing {kind}: {args.wg}", file=sys.stderr)
        print(f"Cache: {cache_dir}", file=sys.stderr)
        print(f"Config: {_gather_plan_summary(args)}", file=sys.stderr)
        if args.clear_cache:
            print("Clear cache: re-downloading all materials.", file=sys.stderr)
        print("-" * 40, file=sys.stderr)

    tracker = StageTracker(stage_plan(args, group_backed), progress)

    # Charter / meetings / WG document list / transcripts — all
    # Datatracker-sourced. Skipped for corpora with no backing group
    # (synthetic and custom): there's no charter, no WG meetings,
    # no auto-discoverable document set.
    meeting_clusters: List[Any] = []
    if group_backed:
        tracker.begin("charter")
        charter_file = paths.charter_path(cache_dir)
        os.makedirs(os.path.dirname(charter_file) or cache_dir, exist_ok=True)
        process_charter(args.wg, charter_file, verbose=verbosity)
        # WG-level metadata (status / area / Additional Resources).
        write_group_info(args.wg, cache_dir, verbose=verbosity)
        # Returns the meeting clusters (date-spans → canonical codes)
        # so transcripts can match interim transcripts to the right
        # clustered meeting rather than orphaning them.
        tracker.begin("meetings")
        meeting_clusters = process_meetings(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
        )

    # Mailing list. Auto-discovery (Datatracker → list name) skipped
    # for synthetic corpora; --mailing-list extras still work.
    tracker.begin("mailing list")

    def _mail_progress(list_name: str, done: int, total: int) -> None:
        # Surface the IMAP download as mid-stage detail so a poller sees this
        # long stage moving (one stage covering tens of thousands of messages).
        tracker.detail(f"{list_name}: {done}/{total} messages downloaded")

    sync_mailing_list(
        args.wg,
        cache_dir,
        months=args.months,
        extra_lists=args.mailing_list,
        auto_discover=group_backed,
        verbose=verbosity,
        on_progress=_mail_progress,
    )

    if group_backed:
        # Transcripts: download, then prepend a meeting-context header
        # to each so chunks deep in a 200KB transcript carry attribution.
        # Interim transcripts (no meeting number) are matched to a
        # meeting cluster by date span; only truly unmatched ones orphan.
        tracker.begin("transcripts")
        process_transcripts(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
            meeting_clusters=meeting_clusters,
        )
        enrich_transcripts(cache_dir, verbose=verbosity)

        # Documents (drafts & RFCs) — only auto-discoverable for real WGs.
        tracker.begin("documents")
        process_documents(
            args.wg,
            cache_dir,
            verbose=verbosity,
            include_related=bool(args.include_related_drafts),
        )
    # Extra drafts added via --draft. These aren't attributed to the WG
    # in the document API (often individual / author submissions the WG
    # is tracking but doesn't own), so they need explicit naming. For
    # synthetic corpora, this is the ONLY draft source. Paired with the
    # generative draft sources (--author / --new-drafts) as one stage.
    if args.draft or args.author or args.new_drafts:
        tracker.begin("drafts")
    if args.draft:
        process_extra_drafts(args.draft, cache_dir, verbose=verbosity)

    _gather_dynamic_drafts(args, cache_dir, persisted, verbosity)

    # Extract text from any PDFs in the cache (slide decks, whiteboards,
    # etc.). Writes a sibling .pdf.txt for each so the chunker picks
    # them up — slides become searchable content rather than invisible
    # binaries.
    tracker.begin("pdf text")
    extract_all_pdfs(cache_dir, verbose=verbosity)

    # GitHub issues — download the raw JSON archives first, but defer
    # rendering the .txt files until after the registry is built so the
    # Author / Comment-by lines can use canonical names.
    if args.github:
        tracker.begin("github archives")
    gh_pending = _download_github_archives(args.github, cache_dir, verbosity)

    # Identity registry — consolidates mail/GitHub/Datatracker/draft
    # surface forms into canonical actors. Built BEFORE the github .txt
    # files are rendered so author lines come out canonical.
    tracker.begin("identity registry")
    registry = build_registry(
        args.wg,
        verbose=verbosity,
        with_datatracker_roles=group_backed,
    )

    if args.github:
        tracker.begin("github issues")
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
    tracker.begin("issue files")
    write_issue_files(args.wg, cache_dir, registry=registry, verbose=verbosity)

    # Per-thread reconstructions (depends on the registry so sender
    # names are already canonical when threads are written).
    tracker.begin("thread files")
    write_thread_files(args.wg, cache_dir, registry=registry, verbose=verbosity)

    # Cross-link drafts to threads / issues that cite them. Scans the
    # per-thread and per-issue .md files we just wrote and emits
    # digests/citations.md plus a citation count per draft that
    # overview can surface inline. Has to run AFTER write_thread_files
    # and write_issue_files; runs BEFORE generate_digests so the
    # overview's documents section can pick up the counts.
    tracker.begin("citations")
    citations_map = scan_citations(cache_dir, verbose=verbosity)
    write_citations_digest(cache_dir, citations_map, verbose=verbosity)
    # --add-mentioned-drafts: pull drafts the corpus cites but doesn't
    # have (reuses the citation scan we just ran).
    _gather_mentioned_drafts(
        args, cache_dir, citations_map.keys(), persisted, verbosity
    )
    # citation_counts() is used by overview at tool-call time; nothing
    # consumes the in-memory map further in the gather pipeline. Kept
    # imported so a future caller can compute it without re-scanning.
    _ = citation_counts

    # People digest
    tracker.begin("people")
    write_people_digest(args.wg, cache_dir, registry, verbose=verbosity)

    # Timeline digest
    tracker.begin("timeline")
    write_timeline_digest(
        args.wg,
        cache_dir,
        registry,
        months=args.months,
        verbose=verbosity,
        group_backed=group_backed,
    )

    # Digests
    tracker.begin("digests")
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
        tracker.begin("embedding index")
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
    http_metrics.persist(wg_cache_dir)  # baseline for cross-run comparison

    if verbosity != Verbosity.QUIET:
        print("-" * 40, file=sys.stderr)
        print(http_metrics.current().summary_line(), file=sys.stderr)
        print(f"Cache populated at {cache_dir}.", file=sys.stderr)
        print(
            f"To export: `ietf-llm-export {args.wg} --destination <dir>` "
            "(or --create <GCP_PROJECT>).",
            file=sys.stderr,
        )
    return True


if __name__ == "__main__":  # pragma: no cover
    main()
