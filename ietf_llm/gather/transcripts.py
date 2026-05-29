import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional
from ..paths import transcripts_dir, transcript_path
from ..utils import LogLevel, Verbosity, log, get_cache_dir

if TYPE_CHECKING:
    from .meetings import MeetingCluster


def process_transcripts(
    wg_name: str,
    destination: str,
    verbose: Verbosity = Verbosity.STATUS,
    months: Optional[int] = None,
    meeting_clusters: "Optional[List[MeetingCluster]]" = None,
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

    # Source filenames look like `IETF125-AIPREF-20260316-0330.md` (a
    # numbered IETF meeting) or `IETF-AIPREF-20260415-1315.md` (an
    # interim — no meeting number). The destination layout puts each
    # transcript under `meetings/<code>/transcripts/<datetime>.md`,
    # using the meeting number as the code when present and the
    # `_orphans` directory when not. (Meetings.py and transcript_context
    # both handle the orphan case downstream.)
    src_pattern = re.compile(
        r"^IETF(?P<meeting_num>\d+)?-"
        + re.escape(wg_upper)
        + r"-(?P<date>\d{8})-(?P<time>\d{4})\.md$",
        re.IGNORECASE,
    )
    for file in os.listdir(transcripts_path):
        match = src_pattern.match(file)
        if not match:
            continue
        meeting_num = match.group("meeting_num")
        date_str = match.group("date")
        time_str = match.group("time")
        # Filtering by date if requested
        if cutoff_date:
            try:
                file_date = datetime.strptime(date_str, "%Y%m%d")
                if file_date < cutoff_date:
                    continue
            except ValueError:
                pass

        src_path = os.path.join(transcripts_path, file)
        # Numbered meetings map straight to `ietf<N>`. Interim
        # transcripts carry no number; match them to a meeting cluster
        # whose date span contains the transcript's date, so they land
        # under the (clustered) interim's dir instead of `_orphans`.
        if meeting_num:
            code: Optional[str] = f"ietf{meeting_num}"
        else:
            code = _match_interim_cluster(date_str, meeting_clusters)
        datetime_token = f"{date_str}{time_str}"
        out_dir = transcripts_dir(destination, code)
        os.makedirs(out_dir, exist_ok=True)
        dest_path = transcript_path(destination, code, datetime_token)

        # Migration: if this transcript previously orphaned (no
        # cluster match before) and now resolves to a real meeting
        # code, drop the stale `_orphans/` copy so it isn't duplicated.
        if code is not None:
            orphan_copy = transcript_path(destination, None, datetime_token)
            if orphan_copy != dest_path and os.path.exists(orphan_copy):
                try:
                    os.remove(orphan_copy)
                except OSError:
                    pass

        if not os.path.exists(dest_path):
            log(
                f"Copying transcript: {os.path.relpath(dest_path, destination)}...",
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


def _match_interim_cluster(
    date_str: str,
    clusters: "Optional[List[MeetingCluster]]",
) -> Optional[str]:
    """Return the canonical code of the meeting cluster whose date span
    contains `date_str` (YYYYMMDD), or None if there's no cluster /
    no match — in which case the transcript orphans as before.
    """
    if not clusters:
        return None
    try:
        when = datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return None
    for cluster in clusters:
        if cluster.covers(when):
            return cluster.code
    return None
