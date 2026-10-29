"""Shape of a GitHub issue/PR archive, enforced where it enters and where it is read.

`github/<repo-slug>.json` is fetched from `https://<owner>.github.io/<repo>/
archive.json`, i.e. content controlled by whoever can push that repo's gh-pages
branch, and discovery tracks every repo in a WG's GitHub org. Its `repo` and each
record's `number` become path components under `issues/` and `pulls/`, so an
unchecked value writes outside the corpus; and a wrong type anywhere (a label that
is an object, a comment that is a string) raises out of a renderer and aborts the
gather.

`normalise_archive` fixes the shape once, at download. `normalise_cached` applies it
to an archive already on disk, which every reader does too: archives written by
earlier gathers are not re-downloaded, and the file is the thing a reader trusts.
The renderers and digests can then assume the shape instead of each guarding it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ...log import LogLevel, Verbosity, log

# GitHub owners carry no dots, and neither `.` nor `..` is a repository name.
_REPO_RE = re.compile(r"[A-Za-z0-9_-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+")
#: Far above any real issue number, and short enough to be a file name.
_MAX_NUMBER = 10**12

#: Scalar fields the renderers call string methods on. A non-string value is
#: dropped, so `.get(k) or ""` reads it as absent.
_RECORD_TEXT = (
    "title",
    "state",
    "author",
    "body",
    "createdAt",
    "updatedAt",
    "closedAt",
    "mergedAt",
    "mergedBy",
    "headRefName",
    "baseRefName",
)
_COMMENT_TEXT = ("author", "body", "createdAt", "state")


def valid_repo(name: object) -> bool:
    """True for `owner/repo` over the characters GitHub allows."""
    return isinstance(name, str) and _REPO_RE.fullmatch(name) is not None


def _number(value: object) -> Optional[int]:
    """A positive issue/PR number from an int or a string of digits, else None."""
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and len(value) < 13
    ):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value < _MAX_NUMBER else None


def _drop_non_text(item: Dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        if item.get(field) is not None and not isinstance(item[field], str):
            del item[field]


def _dicts(
    value: object, text: tuple[str, ...] = _COMMENT_TEXT
) -> List[Dict[str, Any]]:
    """The dict members of `value` (copied, text fields cleaned); `[]` if it is
    not a list."""
    out: List[Dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            item = dict(item)
            _drop_non_text(item, text)
            out.append(item)
    return out


def _labels(value: object) -> List[str]:
    """Label names, whether the archive carries bare strings or `{name: ...}`."""
    names: List[str] = []
    for label in value if isinstance(value, list) else []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _record(raw: object, pull: bool) -> Optional[Dict[str, Any]]:
    """A cleaned copy of one issue or PR, or None when it has no usable number."""
    if not isinstance(raw, dict):
        return None
    number = _number(raw.get("number"))
    if number is None:
        return None
    record = dict(raw)
    record["number"] = number
    _drop_non_text(record, _RECORD_TEXT)
    if "labels" in raw:
        record["labels"] = _labels(raw["labels"])
    if "comments" in raw:
        record["comments"] = _dicts(raw["comments"])
    if pull:
        if "reviews" in raw:
            record["reviews"] = _dicts(raw["reviews"])
            for review in record["reviews"]:
                review["comments"] = _dicts(review.get("comments"))
        if "mergeCommit" in raw and not isinstance(raw["mergeCommit"], dict):
            del record["mergeCommit"]
    return record


def _records(value: object, pull: bool) -> Tuple[List[Dict[str, Any]], List[object]]:
    """`(kept records, the members dropped)` for one `issues` or `pulls` array."""
    kept: List[Dict[str, Any]] = []
    lost: List[object] = []
    for raw in value if isinstance(value, list) else []:
        cleaned = _record(raw, pull)
        if cleaned is None:
            lost.append(raw)
        else:
            kept.append(cleaned)
    return kept, lost


def normalise_archive(
    obj: Any,
    repo: str,
    source: str = "",
    verbose: Verbosity = Verbosity.QUIET,
    level: LogLevel = LogLevel.WARN,
) -> Optional[Dict[str, Any]]:
    """`obj` as an archive of the documented shape, or None if it is unusable.

    Unusable means not a JSON object, or holding records of which none could be
    kept: that is a format change we do not understand, and returning an empty
    archive would replace good data and empty the per-issue files. Callers treat
    None as "no archive".

    `repo` is the name the archive is stored under. Records that cannot be used
    are dropped, with one note per call at `level` naming how many and the
    first.
    """
    where = source or repo
    if not isinstance(obj, dict):
        log(
            f"GitHub archive {where}: not a JSON object; ignored.", verbose, level=level
        )
        return None
    out = dict(obj)
    out["repo"] = repo
    if "timestamp" in obj and not isinstance(obj["timestamp"], str):
        del out["timestamp"]
    if "labels" in obj:
        out["labels"] = _dicts(obj["labels"], ("name", "description"))
    lost: List[object] = []
    kept = 0
    for key, pull in (("issues", False), ("pulls", True)):
        if key in obj:
            out[key], dropped = _records(obj[key], pull)
            kept += len(out[key])
            lost += dropped
    if lost:
        sample = json.dumps(lost[0], default=str)[:80]
        log(
            f"GitHub archive {where}: dropped {len(lost)} malformed record(s), "
            f"first {sample}.",
            verbose,
            level=LogLevel.ERROR if not kept else level,
        )
    return out if kept or not lost else None


def archive_slug(filename: str) -> str:
    """The repo slug a `github/<slug>.json` archive is stored under. Paths are
    built from this, never from the archive's own `repo`: the file name is what
    the writer and the pruner agree on."""
    return filename[: -len(".json")] if filename.endswith(".json") else filename


def normalise_cached(
    obj: Any, filename: str, verbose: Verbosity = Verbosity.QUIET
) -> Optional[Dict[str, Any]]:
    """`normalise_archive` for an archive read back from `github/<filename>`.

    `repo` is for display and URLs only: the archive's own when it is a
    well-formed `owner/repo` (a renamed or transferred repo keeps its real
    name), else the slug. Keep paths off it; use `archive_slug`. Drops are
    reported at PROGRESS, since the download already said so.
    """
    stem = archive_slug(filename)
    claimed = obj.get("repo") if isinstance(obj, dict) else None
    repo = claimed if isinstance(claimed, str) and valid_repo(claimed) else stem
    return normalise_archive(obj, repo, filename, verbose, LogLevel.PROGRESS)


def load_archive(
    path: str, verbose: Verbosity = Verbosity.QUIET, level: LogLevel = LogLevel.ERROR
) -> Optional[Dict[str, Any]]:
    """Read and normalise the archive at `path`, or None (reported at `level`)
    when it cannot be read or parsed. `RecursionError` and the int-digit
    `ValueError` come out of the JSON parser on hostile input and are not
    `JSONDecodeError`s."""
    name = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError, RecursionError) as err:
        log(f"Skipping {name}: {type(err).__name__}: {err}", verbose, level=level)
        return None
    return normalise_cached(obj, name, verbose)
