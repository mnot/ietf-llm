"""Bounded execution of the caller's regex for `grep_corpus`.

Python's `re` runs a pattern in one uninterruptible C call, so a catastrophic
pattern (`(\\w|\\w)*X`) cannot be stopped from inside the process: a thread
cannot be killed, and `_offload` can only abandon it, leaving it to burn CPU
and hold a worker slot for hours. So a user-supplied regex is scanned in a
child process (`python -m ietf_llm.regexscan`) that the parent kills at the
budget. A literal pattern has no such failure mode and is scanned in-process
by the same functions.

This module is deliberately stdlib-only and lives outside `ietf_llm.mcp`: the
child imports it, and importing that package would drag in the whole MCP
server for every scan. Results are plain JSON-able dicts so the in-process and
child paths share one shape. The parent never runs the user's pattern on the
child path — the child returns finished, already-elided lines.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from typing import (
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypedDict,
)

#: Longest rendered line before it is elided. Draft text wraps at ~72 columns,
#: but a pasted log line or a base64 blob in a message can run for thousands.
MAX_LINE_CHARS = 400

#: Seconds past the budget the parent waits before killing the child. The
#: child stops itself between files / sections at the budget; this slack covers
#: interpreter start-up and a final result write, and is what a single stuck
#: regex call costs beyond the budget.
KILL_SLACK_S = 3.0


class ScanTimedOut(Exception):
    """The child outran its budget (killed by the parent, or by its own alarm)."""


class ScanFailed(Exception):
    """The child exited abnormally for a reason other than the budget."""


def elide(line: str, matcher: Optional["re.Pattern[str]"] = None) -> str:
    """Bound one rendered line, keeping the match visible.

    Truncating from the left would render a "hit" line with no match in it
    whenever the match sits past the budget — which is precisely the case this
    cap exists for (a pasted log line, a base64 blob). So when the line is over
    budget we window around the first match instead, marking each elided side.
    """
    stripped = line.rstrip()
    if len(stripped) <= MAX_LINE_CHARS:
        return stripped
    match = matcher.search(stripped) if matcher is not None else None
    if match is None or match.start() < MAX_LINE_CHARS - 1:
        return stripped[: MAX_LINE_CHARS - 1] + "…"
    # Centre the window on the match, clamped to the end of the line.
    half = (MAX_LINE_CHARS - 2) // 2
    start = max(0, match.start() - half)
    end = min(len(stripped), start + MAX_LINE_CHARS - 2)
    return "…" + stripped[start:end] + ("…" if end < len(stripped) else "")


def excerpts(
    text: str, matcher: "re.Pattern[str]", context: int, max_matches: int
) -> List[List[str]]:
    """The lines covering each of the first `max_matches` matches in `text`,
    with `context` lines either side.

    A match may straddle a wrap, so the span is taken from the line holding its
    first character to the line holding its last — rendering only the opening
    line would cut the phrase the caller searched for in half.
    """
    lines = text.splitlines()
    offsets: List[int] = []
    running = 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1
    out: List[List[str]] = []
    for count, match in enumerate(matcher.finditer(text)):
        if count >= max_matches:
            break
        first = max(0, bisect.bisect_right(offsets, match.start()) - 1)
        last = max(
            first, bisect.bisect_right(offsets, max(match.start(), match.end() - 1)) - 1
        )
        low = max(0, first - context)
        high = min(len(lines), last + 1 + context)
        out.append([elide(line) for line in lines[low:high]])
    return out


class FileScan(TypedDict):
    """Outcome of `scan_files`."""

    #: `(relpath, lineno, elided line)`, in (file, line) order.
    hits: List[Tuple[str, int, str]]
    per_file: Dict[str, int]
    total: int
    #: Files read to the end; less than the input length when `complete` is False.
    scanned: int
    complete: bool


def _scan_file(
    path: str,
    relpath: str,
    matcher: "re.Pattern[str]",
    hits: List[Tuple[str, int, str]],
    keep: int,
    per_file_cap: Optional[int],
) -> int:
    """Count every matching line in one file; append the first `keep` of them
    (across the whole scan) to `hits`, and no more than `per_file_cap` from
    this file. Returns the count, which is complete whether or not the lines
    were retained.

    Reads the file whole and prefilters with a single search over the entire
    text: the overwhelmingly common case is a file with no match at all, and
    one C-level scan is far cheaper than splitting a 30 KB message thread into
    lines to check each one.

    Counting past `keep` but not retaining is what lets the reported total stay
    honest without holding a hit per match: a one-character pattern matches
    over a million lines in a real corpus, and keeping them all would be a
    memory-exhaustion lever on a shared server. Files are scanned in relpath
    order, so the retained hits are exactly the ones the renderer would have
    shown after sorting.

    The per-file cap is applied here, before the total `keep` is spent, which
    is what stops a file with hundreds of hits from using up the whole budget
    ahead of the files sorted after it. With a cap, `hits` is therefore no
    longer a prefix of the sorted matches, but it is still in (file, line)
    order.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return 0
    if not matcher.search(text):
        return 0
    found = 0
    kept = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if matcher.search(line):
            found += 1
            if len(hits) < keep and (per_file_cap is None or kept < per_file_cap):
                hits.append((relpath, lineno, elide(line, matcher)))
                kept += 1
    return found


def scan_files(
    matcher: "re.Pattern[str]",
    files: Sequence[Tuple[str, str]],
    keep: int,
    per_file_cap: Optional[int],
    deadline: Optional[float] = None,
) -> FileScan:
    """Scan `files` (`(relpath, path)`, relpath-sorted) for `matcher`.

    `deadline` is a `time.monotonic()` instant, checked between files: past it
    the scan stops and reports `complete=False`, so its counts describe only
    the files read. A single file that never finishes is the parent's kill to
    handle, not this function's."""
    hits: List[Tuple[str, int, str]] = []
    per_file: Dict[str, int] = {}
    total = 0
    scanned = 0
    for relpath, path in files:
        if deadline is not None and time.monotonic() > deadline:
            return FileScan(
                hits=hits,
                per_file=per_file,
                total=total,
                scanned=scanned,
                complete=False,
            )
        found = _scan_file(path, relpath, matcher, hits, keep, per_file_cap)
        scanned += 1
        if found:
            per_file[relpath] = found
            total += found
    return FileScan(
        hits=hits, per_file=per_file, total=total, scanned=scanned, complete=True
    )


class SectionScan(TypedDict):
    """Outcome of `scan_sections`."""

    sections: int
    total: int
    matching_sections: int
    per_file: Dict[str, int]
    #: `(file, section, title, matches, excerpts)`.
    hits: List[Tuple[str, Optional[str], str, int, List[List[str]]]]
    stopped_early: bool
    #: Set when the section stream raised `sqlite3.Error`; the counts above
    #: then describe a partial scan and must not be read as a denominator.
    error: Optional[str]


#: How many matches inside one section are counted between clock reads, so a
#: pattern matching at every position cannot overrun the budget unobserved.
_MATCH_CHECK_EVERY = 1024


def scan_sections(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    matcher: "re.Pattern[str]",
    stream: Generator[Tuple[str, Optional[str], str, str], None, None],
    keep: int,
    context: int,
    deadline: float,
    check_every: int,
    per_file_cap: int,
    matches_per_section: int,
) -> SectionScan:
    """Scan an `iter_sections` stream, keeping up to `keep` hits and at most
    `per_file_cap` per file, each with `matches_per_section` excerpts. Stops
    past `deadline` (polled every `check_every` sections and inside a section's
    match loop); the stream is closed either way."""
    hits: List[Tuple[str, Optional[str], str, int, List[List[str]]]] = []
    kept_per_file: Dict[str, int] = {}
    per_file: Dict[str, int] = {}
    total = 0
    sections = 0
    matching = 0
    stopped = False
    error: Optional[str] = None
    try:
        for file, section, title, text in stream:
            sections += 1
            if not sections % check_every and time.monotonic() > deadline:
                stopped = True
                break
            found = 0
            for _ in matcher.finditer(text):
                found += 1
                if not found % _MATCH_CHECK_EVERY and time.monotonic() > deadline:
                    stopped = True
                    break
            if stopped:
                break
            if not found:
                continue
            matching += 1
            total += found
            per_file[file] = per_file.get(file, 0) + found
            if len(hits) >= keep or kept_per_file.get(file, 0) >= per_file_cap:
                continue
            kept_per_file[file] = kept_per_file.get(file, 0) + 1
            hits.append(
                (
                    file,
                    section,
                    title,
                    found,
                    excerpts(text, matcher, context, matches_per_section),
                )
            )
    except sqlite3.Error as exc:
        error = str(exc)
    finally:
        stream.close()
    return SectionScan(
        sections=sections,
        total=total,
        matching_sections=matching,
        per_file=per_file,
        hits=hits,
        stopped_early=stopped,
        error=error,
    )


# --- the child process --------------------------------------------------------


def run_bounded(request: Dict[str, Any], budget_s: float) -> Dict[str, Any]:
    """Run one scan `request` in a child process and return its result dict.

    The child stops itself at `budget_s`; if it has not answered `KILL_SLACK_S`
    later (stuck inside one regex call) it is killed. Raises `ScanTimedOut` for
    either, `ScanFailed` for any other abnormal exit. The child is always
    reaped before this returns, so a timed-out scan leaves nothing running.

    `request` is `{"kind": "files" | "sections", "pattern": str, "flags": int,
    ...}`; see `_child_main` for the per-kind fields."""
    payload = json.dumps({**request, "budget": budget_s}).encode("utf-8")
    with subprocess.Popen(
        [sys.executable, "-m", "ietf_llm.regexscan"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            out, err = proc.communicate(
                payload, timeout=max(0.0, budget_s) + KILL_SLACK_S
            )
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise ScanTimedOut() from exc
        except BaseException:
            proc.kill()
            raise
    alarm = getattr(signal, "SIGALRM", None)
    if alarm is not None and proc.returncode == -alarm:
        raise ScanTimedOut()
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip()[-300:]
        raise ScanFailed(f"scan process exited {proc.returncode}: {tail}")
    result: Dict[str, Any] = json.loads(out)
    return result


def _arm_self_destruct(budget_s: float) -> None:
    """Have the kernel kill this process if it outlives its budget by a wide
    margin, so a parent that died mid-scan cannot leave a runaway regex behind.
    POSIX only; elsewhere the parent's kill is the only bound."""
    if hasattr(signal, "alarm"):
        signal.alarm(max(1, int(budget_s + 2 * KILL_SLACK_S) + 1))


def _renice() -> None:
    """Yield CPU to the server while a scan runs; best effort, POSIX only."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _child_main() -> int:
    """Read one request from stdin, scan, write the result to stdout.

    `files` requests carry `files` (`[relpath, path]` pairs), `keep` and
    `per_file_cap`. `sections` requests carry `db_path` (resolved by the parent,
    which owns store state), `names` (a filename filter, or null for all),
    `keep`, `context`, `check_every`, `per_file_cap` and `matches_per_section`."""
    request = json.load(sys.stdin)
    budget = float(request["budget"])
    _arm_self_destruct(budget)
    _renice()
    deadline = time.monotonic() + budget
    matcher = re.compile(request["pattern"], int(request["flags"]))
    result: Any
    if request["kind"] == "files":
        result = scan_files(
            matcher,
            [(str(rel), str(path)) for rel, path in request["files"]],
            int(request["keep"]),
            request["per_file_cap"],
            deadline,
        )
    else:
        # Deferred: only a sections scan needs the sqlite/numpy-laden package.
        # pylint: disable-next=import-outside-toplevel
        from .embeddings.storage import iter_sections_at

        result = scan_sections(
            matcher,
            iter_sections_at(request["db_path"], request["names"]),
            int(request["keep"]),
            int(request["context"]),
            deadline,
            int(request["check_every"]),
            int(request["per_file_cap"]),
            int(request["matches_per_section"]),
        )
    json.dump(result, sys.stdout)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_child_main())
