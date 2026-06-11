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
import os
import shutil
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import canonical, cli_list, config, http_metrics, paths, service_config
from .digest import generate_digests
from .digest.timeline import write_timeline_digest
from .embeddings import DEFAULT_EMBED_MODEL, build_index
from .freshness import record_gather
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
    download_github_archives,
    process_github_issues,
    validate_github_repos,
)
from .gather.group_info import write_group_info
from .gather.issue_files import write_issue_files
from .gather.mail_threads import write_thread_files
from .gather.mbox import sync_mailing_list, validate_list_names
from .gather.meetings import process_meetings
from .gather.message_citations import build_message_citations
from .gather.pdf_extract import extract_all_pdfs
from .gather.recent_drafts import fetch_new_draft_names, prune_drafts
from .gather.repo_discovery import autotrack_github, print_discovery
from .gather.rfcs import ensure_rfc_index
from .gather.transcript_context import enrich_transcripts
from .gather.transcripts import process_transcripts
from .gather_cli import build_parser
from .gather_plan import _gather_plan_summary
from .gather_stages import ProgressFn, StageTracker, stage_plan
from .people import build_registry, write_people_digest
from .skill_install import install_skills, sync_if_pristine
from .utils import (
    DEFAULT_MONTHS,
    LogLevel,
    Verbosity,
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

    if args.all:
        targets = cli_list.discover_gathered_wgs()
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

    # Suppress regenerable / non-served bulk when the CLI flag (always set by
    # an MCP gather via to_argv) OR a cloud backend asks for it (a cloud-served
    # version is never grepped locally). Computed once, threaded as a boolean.
    on_cloud = service_config.store_backend() == "cloud"
    suppress_pdf = bool(args.no_pdf) or on_cloud
    suppress_raw = bool(args.no_raw) or on_cloud

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
        suppress_raw=suppress_raw,
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
        enrich_transcripts(cache_dir, verbose=verbosity, wg=args.wg)

        # Documents (drafts & RFCs) — only auto-discoverable for real WGs.
        tracker.begin("documents")
        process_documents(
            args.wg,
            cache_dir,
            verbose=verbosity,
            include_related=bool(args.include_related_drafts),
            include_rfc_bodies=bool(args.rfcs),
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
    extract_all_pdfs(cache_dir, verbose=verbosity, suppress_pdf=suppress_pdf)

    # GitHub issues — download the raw JSON archives first, but defer
    # rendering the .txt files until after the registry is built so the
    # Author / Comment-by lines can use canonical names.
    if args.github:
        tracker.begin("github archives")
    gh_pending = download_github_archives(
        args.github, cache_dir, verbosity, suppress_raw=suppress_raw
    )

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
    # Message-level analogue: resolve archive-permalink URLs in bodies to
    # the gathered message → message_citations.md (same prerequisites).
    tracker.begin("message citations")
    build_message_citations(cache_dir, verbose=verbosity)

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
