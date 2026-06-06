import io
import os
import re
import shutil
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List, Optional

from dulwich import porcelain

from ..paths import transcript_path, transcripts_dir
from ..utils import LogLevel, Verbosity, atomic_open, file_lock, get_cache_dir, log

if TYPE_CHECKING:
    from .meetings import MeetingCluster

# Transcripts live on the `cache` branch of this data repo, one flat markdown
# file per session under `transcripts/`. We sync it with dulwich (a pure-Python
# git client) rather than shelling out to a `git` binary or using the GitHub
# REST API: gather stays a single `pip install` with no external tools (the
# cloud deployment has no git), and the smart-HTTP git protocol dulwich speaks
# is not subject to the REST API's 60/hr rate limit.
_TRANSCRIPTS_REPO_URL = "https://github.com/ietf-minutes/ietf-minutes-data.git"
_TRANSCRIPTS_BRANCH = b"cache"


def _sync_transcripts_repo(repo_dir: str, verbose: Verbosity) -> bool:
    """Clone or update the transcripts data repo in place, in pure Python.

    A shallow (`depth=1`) clone the first time, then an incremental pull —
    only the objects new since the last tip cross the wire, so a routine
    re-gather is cheap. Returns True if a usable `transcripts/` checkout is
    present afterwards. Transcripts are optional, so any failure degrades to
    "no transcripts" (or the existing cached copy) rather than aborting the
    whole gather — hence the guarded broad excepts: dulwich surfaces network
    trouble as several unrelated exception types (protocol, urllib3, socket).
    """
    # The clone is shared across all WGs, so serialise clone/pull across
    # concurrent gathers — two writers in one working tree race on refs.
    with file_lock(f"{repo_dir}.lock"):
        if not os.path.exists(repo_dir):
            log(
                f"Cloning {_TRANSCRIPTS_REPO_URL} "
                f"(branch {_TRANSCRIPTS_BRANCH.decode()})...",
                verbose,
                level=LogLevel.STATUS,
            )
            try:
                porcelain.clone(
                    _TRANSCRIPTS_REPO_URL,
                    repo_dir,
                    branch=_TRANSCRIPTS_BRANCH,
                    depth=1,
                    errstream=io.BytesIO(),
                ).close()
            except Exception as err:  # pylint: disable=broad-except
                log(f"Error cloning transcripts repo: {err}", level=LogLevel.ERROR)
                # Drop any partial checkout so the next gather re-clones clean.
                shutil.rmtree(repo_dir, ignore_errors=True)
                return False
        else:
            log("Updating transcripts repo...", verbose, level=LogLevel.PROGRESS)
            try:
                porcelain.pull(
                    repo_dir,
                    _TRANSCRIPTS_REPO_URL,
                    refspecs=[b"refs/heads/" + _TRANSCRIPTS_BRANCH],
                    depth=1,
                    errstream=io.BytesIO(),
                )
            except Exception as err:  # pylint: disable=broad-except
                # Continue with the cached copy — it is usually still usable.
                log(f"Error updating transcripts repo: {err}", level=LogLevel.ERROR)

    return os.path.isdir(os.path.join(repo_dir, "transcripts"))


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
    repo_dir = os.path.join(get_cache_dir(), "transcripts-repo")
    if not _sync_transcripts_repo(repo_dir, verbose):
        log("No transcripts available.", verbose, level=LogLevel.STATUS)
        return []

    # Find transcripts for the WG. The repo layout is flat:
    # transcripts/IETF{num}-{WG}-{date}-{time}.md
    updated_files = []
    transcripts_path = os.path.join(repo_dir, "transcripts")

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
            except ValueError:
                # Undatable file under an explicit date window: exclude it
                # (fail-safe) rather than slipping it past the cutoff.
                continue
            if file_date < cutoff_date:
                continue

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
                with atomic_open(dest_path) as f_out:
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
