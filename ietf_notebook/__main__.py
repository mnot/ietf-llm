import argparse
import json
import os
import shutil
from typing import Any
from .mbox import sync_mailing_list
from .github import download_github_issues, process_github_issues
from .meetings import process_meetings
from .charter import process_charter
from .drafts import process_documents
from .transcripts import process_transcripts
from .digest import generate_digests
from .utils import (
    Verbosity,
    LogLevel,
    log,
    get_config_dir,
    get_wg_title,
    DEFAULT_MONTHS,
    get_wg_file_cache_dir,
    copy_if_updated,
    get_cache_dir,
)
from .notebooklm import (
    get_credentials,
    create_notebook,
    upload_source,
)


def _default_llm_model(verbose: Verbosity) -> str:
    """Return the user's configured default `llm` model name, or a fallback."""
    try:
        import llm  # pylint: disable=import-outside-toplevel,import-error

        return str(llm.get_default_model())  # type: ignore[no-untyped-call]
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"Could not resolve default llm model ({err}); "
            "falling back to 'claude-haiku-4-5'.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return "claude-haiku-4-5"


def load_config_args(wg_name: str) -> dict[str, Any]:
    """Load persisted arguments for a Working Group."""
    config_file = os.path.join(get_config_dir(), wg_name, "config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as file_handle:
                return dict(json.load(file_handle))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config_args(wg_name: str, args: dict[str, Any]) -> None:
    """Save arguments for a Working Group."""
    wg_config_dir = os.path.join(get_config_dir(), wg_name)
    os.makedirs(wg_config_dir, exist_ok=True)
    config_file = os.path.join(wg_config_dir, "config.json")
    try:
        with open(config_file, "w", encoding="utf-8") as file_handle:
            json.dump(args, file_handle, indent=2)
    except OSError as err:
        log(f"Error saving config: {err}", level=LogLevel.ERROR)


def merge_config_args(args: argparse.Namespace) -> None:
    """Merge and persist configuration arguments."""
    # Handle --clear-config
    if args.clear_config:
        wg_config_dir = os.path.join(get_config_dir(), args.wg)
        if os.path.exists(wg_config_dir):
            if not getattr(args, "quiet", False):
                print(f"Clearing configuration for {args.wg}...")
            shutil.rmtree(wg_config_dir)

    # Load and merge config
    persisted = load_config_args(args.wg)

    # Persistence logic:
    # 1. Scalars: CLI overrides persisted. If not in CLI, use persisted.
    # 2. Lists: CLI extends persisted.

    persistable_scalars = [
        "destination",
        "create",
        "credentials_file",
        "token_file",
        "months",
    ]
    persistable_lists = ["github", "github_label", "exclude_github_label"]

    for key in persistable_scalars:
        val = getattr(args, key)
        # Check if it's the default value for some arguments
        is_default = False
        val = getattr(args, key)
        # Check if it's the default value for some arguments
        is_default = False
        if key == "credentials_file" and val == os.path.join(
            get_config_dir(), "client_secrets.json"
        ):
            is_default = True
        elif key == "token_file" and val == os.path.join(
            get_config_dir(), "token.json"
        ):
            is_default = True
        elif key == "months" and val == DEFAULT_MONTHS:
            is_default = True

        if (val is None or is_default) and key in persisted:
            setattr(args, key, persisted[key])
        elif val is not None and not is_default:
            persisted[key] = val

    for key in persistable_lists:
        cli_vals = getattr(args, key) or []
        persisted_vals = persisted.get(key, [])
        # Migration: if single string, convert to list
        if isinstance(persisted_vals, str):
            persisted_vals = [persisted_vals]
        combined = list(set(persisted_vals + cli_vals))
        setattr(args, key, combined if combined else None)
        if combined:
            persisted[key] = combined

    # Save updated config
    save_config_args(args.wg, persisted)


def export_to_notebooklm(
    args: argparse.Namespace, cache_dir: str, verbosity: Verbosity
) -> None:
    """Upload cached documents to a new NotebookLM notebook."""
    gcp_project = args.create
    print("-" * 40)
    print("Exporting to NotebookLM...")

    creds = get_credentials(args.credentials_file, args.token_file, verbose=verbosity)
    if not creds:
        log("Authentication failed.", verbosity, level=LogLevel.ERROR)
        return

    wg_title = get_wg_title(args.wg)
    notebook_title = f"IETF {wg_title} Working Group"
    notebook_id = create_notebook(gcp_project, notebook_title, creds, verbose=verbosity)

    if not notebook_id:
        log("Failed to create notebook.", verbosity, level=LogLevel.ERROR)
        return

    success_count = 0
    # When creating a new notebook, upload all relevant text files from the CACHE.
    all_cache_files = [
        os.path.join(cache_dir, f)
        for f in os.listdir(cache_dir)
        if f.endswith((".txt", ".md"))
    ]
    for file_path in sorted(list(set(all_cache_files))):
        if upload_source(
            gcp_project,
            notebook_id,
            file_path,
            creds,
            verbose=verbosity,
        ):
            success_count += 1

    if success_count > 0:
        print(
            f"Successfully uploaded {success_count} files "
            f"to notebook '{notebook_title}'."
        )
    else:
        log(
            "No files were uploaded to the notebook.",
            verbosity,
            level=LogLevel.ERROR,
        )


def main() -> None:  # pylint: disable=too-many-branches
    parser = argparse.ArgumentParser(
        description="Automate creation of NotebookLM-ready documents for an IETF Working Group."
    )
    parser.add_argument("wg", help="IETF Working Group short name (e.g., 'httpbis')")
    parser.add_argument(
        "--github",
        action="append",
        help="GitHub owner/repo, can be specified multiple times",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=DEFAULT_MONTHS,
        help=f"Number of months of materials and emails to fetch (default: {DEFAULT_MONTHS})",
    )
    parser.add_argument(
        "--github-label",
        action="append",
        help="Include only GitHub issues with this label (can be specified multiple times)",
    )
    parser.add_argument(
        "--exclude-github-label",
        action="append",
        help="Exclude GitHub issues with this label (can be specified multiple times)",
    )
    parser.add_argument(
        "--destination",
        help="Destination folder for exported documents (required on first run)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Only output errors")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Detailed progress reporting"
    )

    parser.add_argument(
        "--create",
        metavar="GCP_PROJECT_ID",
        help="Upload the generated files to a new notebook in NotebookLM",
    )
    parser.add_argument(
        "--clear-config",
        action="store_true",
        help="Clear the persisted configuration for this Working Group",
    )
    parser.add_argument(
        "--credentials-file",
        default=os.path.join(get_config_dir(), "client_secrets.json"),
        help="Path to the Google Cloud OAuth client secrets file",
    )
    parser.add_argument(
        "--token-file",
        default=os.path.join(get_config_dir(), "token.json"),
        help="Path to the Google Cloud OAuth token file",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear the local file cache for this Working Group and start fresh.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Only write updated files to destination.",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Add LLM-generated one-line summaries to digest files. "
        "Requires the `llm` package (https://llm.datasette.io/) and a "
        "configured model.",
    )
    parser.add_argument(
        "--summarize-model",
        metavar="MODEL",
        help="Model id for --summarize (e.g. 'claude-haiku-4-5', "
        "'gpt-4o-mini'). Defaults to `llm`'s configured default.",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="After gather, build/refresh the semantic search index for this "
        "WG. Requires the `llm` and `numpy` packages (install with the "
        "`search` extra).",
    )
    parser.add_argument(
        "--embed-model",
        metavar="MODEL",
        help="Embedding model id for --embed (e.g. '3-small'). Defaults to "
        "OpenAI text-embedding-3-small.",
    )
    parser.add_argument(
        "--rebuild-embeddings",
        action="store_true",
        help="With --embed, drop and re-embed all files instead of "
        "incrementally updating.",
    )

    args = parser.parse_args()

    merge_config_args(args)

    # --destination is optional. Without it, the gather still populates the
    # cache at ~/.cache/ietf-notebook/<wg>/, which is what the MCP server,
    # `ietf-notebook-search`, and `--create` (NotebookLM upload) all read
    # from. A destination is only needed if you want a clean directory of
    # text/md files to upload to NotebookLM by hand.
    if args.destination:
        os.makedirs(args.destination, exist_ok=True)

    # 1. Handle --clear-cache
    wg_cache_dir = os.path.join(get_cache_dir(), args.wg)
    cache_dir = get_wg_file_cache_dir(args.wg)
    if args.clear_cache:
        log(f"Clearing cache for {args.wg}...", Verbosity.STATUS)
        if os.path.exists(wg_cache_dir):
            shutil.rmtree(wg_cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

    verbosity = Verbosity.STATUS
    if args.quiet:
        verbosity = Verbosity.QUIET
    elif args.verbose:
        verbosity = Verbosity.VERBOSE

    if verbosity != Verbosity.QUIET:
        print(f"Processing WG: {args.wg}")
        if args.destination:
            print(f"Destination: {args.destination}")
        else:
            print("Destination: (cache-only; no mirror)")
        if args.clear_cache:
            print("Clear cache: Re-downloading all materials.")
        else:
            print("Default mode: Using local cache for existing materials.")
        print("-" * 40)

    # We will collect all files generated by the processors in the cache.
    # We then mirror them to the destination.
    generated_cache_files = []

    # 1. Charter
    charter_file = os.path.join(cache_dir, f"{args.wg}-charter.txt")
    generated_cache_files.extend(
        process_charter(args.wg, charter_file, verbose=verbosity)
    )

    # 2. Meetings
    generated_cache_files.extend(
        process_meetings(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
        )
    )

    # 3. Mailing List
    generated_cache_files.extend(
        sync_mailing_list(args.wg, cache_dir, months=args.months, verbose=verbosity)
    )

    # 4. Transcripts
    generated_cache_files.extend(
        process_transcripts(
            args.wg,
            cache_dir,
            verbose=verbosity,
            months=args.months,
        )
    )

    # 5. Documents (Drafts & RFCs)
    generated_cache_files.extend(
        process_documents(args.wg, cache_dir, verbose=verbosity)
    )

    # 6. GitHub Issues
    if args.github:
        for repo_short in args.github:
            # Create a slug for the repository name (handle both owner/repo and absolute URLs)
            if repo_short.startswith("http"):
                repo_slug = repo_short.split("/")[-1].replace(".json", "")
            else:
                repo_slug = repo_short.replace("/", "-")

            gh_json = os.path.join(cache_dir, f"{args.wg}-github-{repo_slug}.json")
            gh_txt = os.path.join(cache_dir, f"{args.wg}-github-{repo_slug}.txt")

            if download_github_issues(repo_short, gh_json, verbose=verbosity):
                generated_cache_files.append(gh_json)
                generated_cache_files.extend(
                    process_github_issues(
                        gh_json,
                        gh_txt,
                        include_labels=args.github_label,
                        exclude_labels=args.exclude_github_label,
                        verbose=verbosity,
                    )
                )

    # 6b. Digests (index + issues + threads)
    summarize_model: Any = None
    if args.summarize or args.summarize_model:
        # If user passed --summarize-model, use it; else None -> llm default
        summarize_model = args.summarize_model or _default_llm_model(verbosity)
    generated_cache_files.extend(
        generate_digests(
            args.wg,
            cache_dir,
            summarize_model=summarize_model,
            verbose=verbosity,
        )
    )

    # 7. Mirror to destination (if one was supplied; otherwise everything
    # is already in the cache for the MCP server / search CLI to read).
    updated_files = []
    if args.destination:
        for src in sorted(list(set(generated_cache_files))):
            if not os.path.exists(src):
                continue
            if src.endswith(".json"):  # Don't mirror internal JSON
                continue
            filename = os.path.basename(src)
            dst = os.path.join(args.destination, filename)
            if copy_if_updated(src, dst):
                updated_files.append(dst)
            elif args.update:
                # If --update is specified, we want ONLY changed files.
                # So if it's the same, it's not "newly changed", so remove
                # it from destination.
                if os.path.exists(dst):
                    os.remove(dst)

    # 8. Embedding index (opt-in)
    if args.embed:
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

    if args.create:
        export_to_notebooklm(args, cache_dir, verbosity)

    if verbosity != Verbosity.QUIET:
        print("-" * 40)
        if args.destination:
            if updated_files:
                print(f"Updated {len(updated_files)} files in {args.destination}.")
            else:
                print("No files updated in destination.")
        else:
            print("Cache populated; no destination mirror requested.")
        print("All tasks completed.")


if __name__ == "__main__":
    main()
