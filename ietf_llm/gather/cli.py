"""Argument parser for the `ietf-llm` gather CLI.

Split out of `cli/main.py` so the flag surface lives in one focused module:
both `main()` and the programmatic entry point (`run_gather`, used by the
MCP gather runner) build the same fully-defaulted Namespace from it.
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..cli.completion import wg_completer
from ..months import DEFAULT_MONTHS


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
        "Optional when using --install-skills or --all.",
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
        help="Refresh every gathered corpus the configured store knows about, "
        "using each one's persisted gather config. Mutually exclusive with a "
        "positional NAME argument; --clear-config is refused in this mode.",
    )
    parser.add_argument(
        "--used-within",
        type=int,
        metavar="DAYS",
        dest="used_within",
        help="With --all, refresh only corpora read within the last DAYS days "
        "(by the MCP read tools). Corpora with no recorded access fall back to "
        "their last-gathered time, so a freshly gathered corpus is not skipped. "
        "Lets a cron keep active corpora fresh without refreshing zombies.",
    )
    parser.add_argument(
        "--github",
        action="append",
        metavar="OWNER/REPO",
        help="GitHub repo whose issues and pull requests should be gathered "
        "(repeat for multiple).",
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
        "auto-discovers (repeat for multiple). For a WG/RG you usually "
        "don't need this — its own list is discovered from Datatracker and "
        "synced automatically; use this only for *extra* lists the effort "
        "follows. Any list archived at mailarchive.ietf.org works — IETF, "
        "IRTF, or RFC-Editor — synced from the IETF IMAP mirror. A bare name "
        "or a full address; the domain is optional and ignored (`rswg`, "
        "`rswg@rfc-editor.org`). Persisted; future runs without "
        "--mailing-list still sync it.",
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
        "--no-raw",
        action="store_true",
        help="Don't write the regenerable raw/ text dumps (merged "
        "mail-archive-<year>.txt and github-<repo>.txt) — never indexed or "
        "read by a tool. Auto-enabled for MCP gathers and the cloud backend.",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="Don't keep slide-deck .pdf sources: still extract each "
        "deck's .pdf.txt (the served, indexed content), then drop the .pdf. "
        "Auto-enabled for MCP gathers and the cloud backend; local keeps them.",
    )
    parser.add_argument(
        "--seed",
        dest="seed",
        action="store_true",
        default=None,
        help="Re-enable seeding from the public seed store (persists across "
        "gathers). Undoes a previous --no-seed. See --no-seed.",
    )
    parser.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        default=None,
        help="Stop seeding from the public seed store, and remember it: this "
        "persists across gathers until you pass --seed (issue #182). By default "
        "a gather of a covered corpus fetches a prebuilt snapshot over the "
        "network from the seed store (IETF_LLM_SEED_URL) and freshens it, and "
        "`list_corpora` looks the catalog up there; --no-seed turns both off so "
        "gathers run fully cold and offline.",
    )
    parser.add_argument(
        "--refresh-base",
        action="store_true",
        help="Re-pull the seed base even when a local copy exists and is not "
        "stale, replacing it before freshening. Normally seeding only jumps a "
        "cold or stale-relative-to-the-snapshot corpus forward. This is an "
        "explicit override that pulls the snapshot as-is; if your window is "
        "wider or you carry extra sources, freshen re-fetches the difference.",
    )
    parser.add_argument(
        "--rfcs",
        action="store_true",
        help="Also download the WG's published RFC bodies into drafts/ and "
        "index them. Off by default: RFC metadata and full text are always "
        "available globally via search_rfc_index / get_rfc_info, so mirroring them into "
        "every corpus is wasted gather and embed time. The WG's RFC list "
        "still appears in the overview regardless.",
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
        "--init",
        action="store_true",
        dest="init_machine",
        help="Set this machine up without gathering anything: refresh the "
        "RFC-series metadata mirror and the effort catalog, install the full "
        "text of the RFC series from the seed store (about 285 MB, so "
        "`search_rfc_text` and `get_rfc_section` work), and sync the norms "
        "skills. Every gather does all of this afterwards as housekeeping; "
        "this is for the machine that never gathers -- a read-only MCP "
        "deployment, or a first run before you have chosen a corpus. Then "
        "exits. Does not gather.",
    )
    parser.add_argument(
        "--install-skills",
        action="store_true",
        help="Install the norms skills (ietf-interpreting + ietf-contributing) "
        "into every supported agent harness detected on this machine (Claude "
        "Code, Codex, Gemini CLI, opencode) and exit. A convenience for the "
        "same skills you can install yourself from mnot/ietf-skill (their "
        "canonical home). Overwrites any existing copy. Does not gather.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Only output errors."
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Detailed progress reporting."
    )
    return parser
