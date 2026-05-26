import os
import subprocess
from datetime import datetime, timedelta
from typing import List, Optional
from .utils import LogLevel, Verbosity, log, get_cache_dir


def process_transcripts(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
    months: Optional[int] = None,
) -> List[str]:
    """
    Fetch transcripts for a WG from the ietf-minutes-data repo and write to destination.
    """
    repo_url = "https://github.com/ietf-minutes/ietf-minutes-data.git"
    repo_dir = os.path.join(get_cache_dir(), "transcripts-repo")
    branch = "cache"

    # 1. Sync the repo
    if not os.path.exists(repo_dir):
        log(f"Cloning {repo_url} (branch {branch})...", verbose, level=LogLevel.STATUS)
        try:
            subprocess.run(
                ["git", "clone", "-b", branch, "--depth", "1", repo_url, repo_dir],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as err:
            log(f"Error cloning transcripts repo: {err.stderr}", level=LogLevel.ERROR)
            return []
    else:
        log("Updating transcripts repo...", verbose, level=LogLevel.PROGRESS)
        try:
            subprocess.run(
                ["git", "-C", repo_dir, "pull", "origin", branch],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as err:
            log(f"Error updating transcripts repo: {err.stderr}", level=LogLevel.ERROR)
            # Continue anyway, maybe the cache is usable

    # 2. Find transcripts for the WG
    # The repo structure is: transcripts/IETF{num}-{WG}-{date}-{time}.md
    updated_files = []
    transcripts_path = os.path.join(repo_dir, "transcripts")

    if not os.path.exists(transcripts_path):
        log(f"Transcripts directory not found in {repo_dir}", level=LogLevel.ERROR)
        return []

    # WG name in the filename is uppercase in the repo (e.g., AIPREF)
    wg_upper = wg_name.upper()
    cutoff_date = (
        datetime.now() - timedelta(days=months * 30) if months is not None else None
    )

    for file in os.listdir(transcripts_path):
        # Pattern: IETF{num}-{WG}-{date}-{time}.md
        # Example: IETF125-AIPREF-20260316-0330.md
        if file.startswith("IETF") and f"-{wg_upper}-" in file and file.endswith(".md"):
            # Filtering by date if requested
            if cutoff_date:
                try:
                    parts = file.split("-")
                    # parts[2] should be the date YYYYMMDD
                    date_str = parts[2]
                    file_date = datetime.strptime(date_str, "%Y%m%d")
                    if file_date < cutoff_date:
                        continue
                except (IndexError, ValueError):
                    # If parsing fails, fall back to including it
                    pass

            src_path = os.path.join(transcripts_path, file)

            # Destination filename: lowercase and append -transcript
            # Example: ietf125-aipref-20260316-0330-transcript.md
            name, ext = os.path.splitext(file)
            dest_filename = f"{name.lower()}-transcript{ext}"
            dest_path = os.path.join(destination, dest_filename)

            if not os.path.exists(dest_path):
                log(
                    f"Copying transcript: {dest_filename}...",
                    verbose,
                    level=LogLevel.PROGRESS,
                )
                try:
                    with open(src_path, "r", encoding="utf-8") as f_in:
                        content = f_in.read()
                    with open(dest_path, "w", encoding="utf-8") as f_out:
                        f_out.write(content)
                    updated_files.append(dest_path)
                except OSError as err:
                    log(f"Error copying transcript {file}: {err}", level=LogLevel.ERROR)

    if not updated_files:
        log(
            f"No transcripts found for {wg_name} in the data repo.",
            verbose,
            level=LogLevel.STATUS,
        )

    return updated_files
