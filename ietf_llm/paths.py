"""Cache directory layout helpers.

Single source of truth for where files live under a WG's cache. The
new layout (since the flat-layout reorg) groups by artefact kind and
drops the `<wg>-` prefix everywhere because we're already inside the
WG's cache directory.

  <wg>/files/
    charter.txt
    digests/                       (was <wg>-_*.md)
      index.md issues.md threads.md people.md timeline.md
    drafts/
      draft-*.txt, rfc*.txt
    meetings/
      <code>/
        minutes.md                 (was <code>-minutes.md)
        slides/
          <slug>.pdf, <slug>.pdf.txt
        transcripts/
          <YYYYMMDDHHmm>.md        (was <code>-<wg>-<dt>-transcript.md)
        polls/
          <YYYYMMDDHHmm>.md        (was <wg>-polls-<code>-<dt>.md)
      _orphans/                    (transcripts without a meeting code)
        transcripts/
          <YYYYMMDDHHmm>.md
    threads/
      <date>-<slug>.md             (was <wg>-thread-<date>-<slug>.md)
    issues/
      <repo-slug>/
        <N>.md                     (was <wg>-issue-<repo-slug>-<N>.md)
    github/
      <repo-slug>.json             (raw archive)
    ballots/
      <draft-name>.md              (IESG ballot positions per draft)
    raw/                           (NOT indexed; grep / NotebookLM only)
      mail-archive-<YYYY>.txt      (was <wg>-mail-archive-<YYYY>.txt)
      github-<repo-slug>.txt       (was <wg>-github-<repo-slug>.txt)

Embeddings DB and the `last-gathered` sentinel live at <wg>/ (one
level above files/) — they're machinery, not corpus.

Relative paths from `files/` are what the chunker stores in
`chunks.file` and what consumers pass to `read_file_section` /
`get_chunk_text`. Subdirectories are part of the path, e.g.
`meetings/ietf125/minutes.md`.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

# Top-level subdirectories under <wg>/files/.
DIR_DIGESTS = "digests"
DIR_DRAFTS = "drafts"
DIR_MEETINGS = "meetings"
DIR_THREADS = "threads"
DIR_ISSUES = "issues"
DIR_GITHUB = "github"
DIR_RAW = "raw"
DIR_BALLOTS = "ballots"

# Per-meeting subdirs.
SUBDIR_SLIDES = "slides"
SUBDIR_TRANSCRIPTS = "transcripts"
SUBDIR_POLLS = "polls"

# Pseudo meeting code for transcripts that don't match any meeting
# code or minutes file by date. Sits alongside real meeting dirs.
ORPHAN_MEETING_CODE = "_orphans"


# --- Path constructors --------------------------------------------------


def charter_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "charter.txt")


def digest_path(cache_dir: str, kind: str) -> str:
    """Path to a digest file. `kind` is `index`, `issues`, etc."""
    return os.path.join(cache_dir, DIR_DIGESTS, f"{kind}.md")


def drafts_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_DRAFTS)


def threads_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_THREADS)


def thread_path(cache_dir: str, slug: str) -> str:
    """Per-thread file. `slug` is the date-prefixed slug, e.g.
    `2026-05-15-introduction`."""
    return os.path.join(cache_dir, DIR_THREADS, f"{slug}.md")


def issues_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_ISSUES)


def issue_repo_dir(cache_dir: str, repo: str) -> str:
    """Per-issue files for a repo live under issues/<repo-slug>/."""
    return os.path.join(cache_dir, DIR_ISSUES, _repo_slug(repo))


def issue_path(cache_dir: str, repo: str, number: Any) -> str:
    return os.path.join(issue_repo_dir(cache_dir, repo), f"{number}.md")


def meetings_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_MEETINGS)


def meeting_dir(cache_dir: str, code: str) -> str:
    return os.path.join(cache_dir, DIR_MEETINGS, code)


def minutes_path(cache_dir: str, code: str) -> str:
    return os.path.join(meeting_dir(cache_dir, code), "minutes.md")


def agenda_path(cache_dir: str, code: str) -> str:
    return os.path.join(meeting_dir(cache_dir, code), "agenda.md")


def slides_dir(cache_dir: str, code: str) -> str:
    return os.path.join(meeting_dir(cache_dir, code), SUBDIR_SLIDES)


def slide_path(cache_dir: str, code: str, slug: str) -> str:
    """A single slide PDF. `slug` is the slide deck name (without .pdf)."""
    return os.path.join(slides_dir(cache_dir, code), f"{slug}.pdf")


def transcripts_dir(cache_dir: str, code: Optional[str]) -> str:
    """Where transcripts for a meeting live. None / empty `code`
    routes to the `_orphans/transcripts/` dir."""
    real_code = code or ORPHAN_MEETING_CODE
    return os.path.join(meeting_dir(cache_dir, real_code), SUBDIR_TRANSCRIPTS)


def transcript_path(cache_dir: str, code: Optional[str], datetime_token: str) -> str:
    """Per-session transcript. `datetime_token` is the
    `YYYYMMDDHHmm` form from the source filename."""
    return os.path.join(transcripts_dir(cache_dir, code), f"{datetime_token}.md")


def polls_dir(cache_dir: str, code: str) -> str:
    return os.path.join(meeting_dir(cache_dir, code), SUBDIR_POLLS)


def poll_path(cache_dir: str, code: str, datetime_token: str) -> str:
    return os.path.join(polls_dir(cache_dir, code), f"{datetime_token}.md")


def ballots_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_BALLOTS)


def ballot_path(cache_dir: str, doc_name: str) -> str:
    """Per-draft IESG ballot file. `doc_name` is the canonical draft
    basename without revision suffix (e.g. `draft-ietf-tls-rfc8446bis`)."""
    return os.path.join(cache_dir, DIR_BALLOTS, f"{doc_name}.md")


def github_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_GITHUB)


def github_archive_path(cache_dir: str, repo: str) -> str:
    return os.path.join(github_dir(cache_dir), f"{_repo_slug(repo)}.json")


def raw_dir(cache_dir: str) -> str:
    return os.path.join(cache_dir, DIR_RAW)


def raw_mail_archive_path(cache_dir: str, year: int) -> str:
    return os.path.join(raw_dir(cache_dir), f"mail-archive-{year}.txt")


def raw_github_text_path(cache_dir: str, repo: str) -> str:
    return os.path.join(raw_dir(cache_dir), f"github-{_repo_slug(repo)}.txt")


# --- Predicates -----------------------------------------------------------


def is_digest_relpath(relpath: str) -> bool:
    """True if a relative path identifies one of our digest files."""
    return relpath.startswith(f"{DIR_DIGESTS}/") and relpath.endswith(".md")


def digest_kind_from_relpath(relpath: str) -> Optional[str]:
    """Extract `kind` from `digests/<kind>.md`, or None."""
    if not is_digest_relpath(relpath):
        return None
    return relpath[len(DIR_DIGESTS) + 1 : -len(".md")] or None


def is_issue_relpath(relpath: str) -> bool:
    return relpath.startswith(f"{DIR_ISSUES}/") and relpath.endswith(".md")


def is_thread_relpath(relpath: str) -> bool:
    return relpath.startswith(f"{DIR_THREADS}/") and relpath.endswith(".md")


def is_transcript_relpath(relpath: str) -> bool:
    """Transcripts live in any `meetings/<code>/transcripts/<dt>.md`."""
    return (
        relpath.startswith(f"{DIR_MEETINGS}/")
        and f"/{SUBDIR_TRANSCRIPTS}/" in relpath
        and relpath.endswith(".md")
    )


def is_minutes_relpath(relpath: str) -> bool:
    """Minutes are `meetings/<code>/minutes.md`."""
    if not relpath.startswith(f"{DIR_MEETINGS}/"):
        return False
    return relpath.endswith("/minutes.md")


def is_raw_relpath(relpath: str) -> bool:
    """raw/* files are not indexed."""
    return relpath.startswith(f"{DIR_RAW}/")


def is_github_archive_relpath(relpath: str) -> bool:
    return relpath.startswith(f"{DIR_GITHUB}/") and relpath.endswith(".json")


# --- Helpers --------------------------------------------------------------


def _repo_slug(repo: str) -> str:
    """`owner/repo` → `owner-repo`, lowercased. Used for both issue
    subdir names and the raw github archive filename."""
    return repo.replace("/", "-").lower()


def meeting_code_for_relpath(relpath: str) -> Optional[str]:
    """Extract `<code>` from any `meetings/<code>/…` relative path."""
    if not relpath.startswith(f"{DIR_MEETINGS}/"):
        return None
    rest = relpath[len(DIR_MEETINGS) + 1 :]
    if "/" not in rest:
        return rest if rest else None
    return rest.split("/", 1)[0]


# Meeting-code label parsers, shared by slide / transcript context
# headers. Three recognised shapes:
#   ietf<N>             → "IETF N meeting"
#   interim<YYYYMMDD>   → "Interim YYYY-MM-DD"   (clustered interims,
#                         coded by start date; WG is implicit in path)
#   interim<year>…<seq> → "Interim <year> #<seq>"  (legacy per-session
#                         codes in older caches)
# Anything else is returned verbatim. The date branch is tried before
# the legacy one — `interim20260414` is 8 pure digits and matches the
# date shape; `interim2026aipref05` has letters and falls through.
_LABEL_IETF_RE = re.compile(r"^ietf(\d+)$", re.IGNORECASE)
_LABEL_INTERIM_DATE_RE = re.compile(r"^interim(\d{8})$", re.IGNORECASE)
_LABEL_INTERIM_SEQ_RE = re.compile(r"^interim(\d{4})\w*?(\d+)$", re.IGNORECASE)


def meeting_label(code: str) -> str:
    """Human-readable label for a meeting `<code>` (for context headers)."""
    match = _LABEL_IETF_RE.match(code)
    if match:
        return f"IETF {match.group(1)} meeting"
    match = _LABEL_INTERIM_DATE_RE.match(code)
    if match:
        ymd = match.group(1)
        return f"Interim {ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    match = _LABEL_INTERIM_SEQ_RE.match(code)
    if match:
        return f"Interim {match.group(1)} #{match.group(2)}"
    return code
