"""In-process background gather runner for the MCP server.

The MCP server is otherwise read-only and never touches the network; this
module is the one deliberate exception, gated behind
`IETF_LLM_ENABLE_GATHER=1` (see `mcp_server._gather_enabled`). It runs the
same pipeline as the `ietf-llm` CLI in a daemon thread, so a `start_gather`
tool call returns immediately, and records stage-level progress to a
per-corpus status file (`~/.cache/ietf-llm/<corpus>/gather-status.json`)
that the `gather_status` tool reads back.

Concurrency model (matching the rest of the project): one gather per corpus
at a time, different corpora in parallel. A non-blocking per-corpus
`file_lock` enforces this and also guards against a CLI gather of the same
corpus running at the same time; an in-process registry answers "already
running here?" without touching the filesystem.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .utils import (
    LockHeld,
    LogLevel,
    Verbosity,
    atomic_open,
    file_lock,
    get_cache_dir,
    log,
)

_STATUS_NAME = "gather-status.json"
_LOCK_NAME = ".gather.lock"

# A corpus name becomes a cache directory, so it must be a single safe path
# segment: letters/digits/`.`/`-`/`_`, starting with an alphanumeric. This
# bars path separators, `..`, leading dots/dashes, and whitespace — the
# write-side mirror of the read-side `_safe_path` guard, applied *before*
# any path is built (the runner's first act is to take a per-corpus lock,
# which creates directories). Real corpus names (WG shortnames, list names,
# `x-` synthetics) all satisfy it; unusual custom labels still go via the CLI.
_VALID_CORPUS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_CORPUS_LEN = 128

# Live gather threads keyed by corpus, so a second start() in this process
# can answer "already running" without racing on the file lock.
_jobs: Dict[str, threading.Thread] = {}
_registry_lock = threading.Lock()


def valid_corpus_name(name: str) -> bool:
    """True if `name` is a safe single path segment usable as a cache dir."""
    return (
        bool(name) and len(name) <= _MAX_CORPUS_LEN and bool(_VALID_CORPUS.match(name))
    )


def _pid_alive(pid: int) -> bool:
    """Best-effort: is a process with this pid still around? Used to detect a
    `running` status orphaned by a server restart/kill (the gather thread is
    a daemon, so it dies without writing a terminal status)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not signalable by us (EPERM), or unsupported
    return True


@dataclass
class GatherSpec:
    """A gather request: corpus name plus the same optional sources/scope
    the `ietf-llm` CLI accepts. The pipeline classifies the shape (group /
    list / custom / synthetic) from these — callers don't say which."""

    corpus: str
    mailing_list: List[str] = field(default_factory=list)
    draft: List[str] = field(default_factory=list)
    github: List[str] = field(default_factory=list)
    github_label: List[str] = field(default_factory=list)
    exclude_github_label: List[str] = field(default_factory=list)
    author: Optional[str] = None
    new_drafts: bool = False
    months: Optional[int] = None
    add_mentioned_drafts: bool = False
    include_related_drafts: bool = False

    def to_argv(self) -> List[str]:
        """Render as CLI-style argv for `__main__.run_gather`."""
        argv: List[str] = [self.corpus]
        for value in self.mailing_list:
            argv += ["--mailing-list", value]
        for value in self.draft:
            argv += ["--draft", value]
        for value in self.github:
            argv += ["--github", value]
        for value in self.github_label:
            argv += ["--github-label", value]
        for value in self.exclude_github_label:
            argv += ["--exclude-github-label", value]
        if self.author:
            argv += ["--author", self.author]
        if self.new_drafts:
            argv.append("--new-drafts")
        if self.months is not None:
            argv += ["--months", str(self.months)]
        if self.add_mentioned_drafts:
            argv.append("--add-mentioned-drafts")
        if self.include_related_drafts:
            argv.append("--include-related-drafts")
        return argv

    def to_dict(self) -> Dict[str, Any]:
        """The non-default fields, for embedding in the status file."""
        out: Dict[str, Any] = {"corpus": self.corpus}
        for key in (
            "mailing_list",
            "draft",
            "github",
            "github_label",
            "exclude_github_label",
        ):
            value = getattr(self, key)
            if value:
                out[key] = list(value)
        for key in (
            "author",
            "new_drafts",
            "months",
            "add_mentioned_drafts",
            "include_related_drafts",
        ):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status_path(corpus: str) -> str:
    return os.path.join(get_cache_dir(), corpus, _STATUS_NAME)


def _lock_path(corpus: str) -> str:
    return os.path.join(get_cache_dir(), corpus, _LOCK_NAME)


def start(spec: GatherSpec) -> Dict[str, Any]:
    """Start a background gather for `spec.corpus`.

    Returns `{"started": True, "corpus": ...}` when a fresh gather was
    launched, or `{"started": False, "reason": "already running", ...}` if
    one is already in flight for this corpus (in this process or another).
    Returns immediately; progress is tracked via the status file.
    """
    corpus = spec.corpus
    # Validate before any path is constructed: _run's first act is to take a
    # per-corpus file_lock, which makedirs the (corpus-derived) lock path.
    if not valid_corpus_name(corpus):
        return {"started": False, "reason": "invalid name", "corpus": corpus}
    with _registry_lock:
        existing = _jobs.get(corpus)
        if existing is not None and existing.is_alive():
            return {"started": False, "reason": "already running", "corpus": corpus}
        got_lock = threading.Event()
        outcome: Dict[str, bool] = {"locked": False}
        thread = threading.Thread(
            target=_run,
            args=(spec, got_lock, outcome),
            name=f"gather:{corpus}",
            daemon=True,
        )
        _jobs[corpus] = thread
        thread.start()
    # The non-blocking lock acquire inside the thread is immediate; wait
    # briefly for the handshake so we can report the contended case
    # synchronously rather than spawning a no-op thread.
    got_lock.wait(timeout=5.0)
    if not outcome["locked"]:
        return {"started": False, "reason": "already running", "corpus": corpus}
    return {"started": True, "corpus": corpus}


def _run(
    spec: GatherSpec,
    got_lock: threading.Event,
    outcome: Dict[str, bool],
) -> None:
    """Thread body: take the per-corpus lock and run the pipeline."""
    corpus = spec.corpus
    try:
        with file_lock(_lock_path(corpus), blocking=False):
            outcome["locked"] = True
            got_lock.set()
            _execute(spec)
    except LockHeld:
        # Another process holds the corpus lock — leave its status alone.
        pass
    finally:
        got_lock.set()  # unblock start() even on an early/unexpected error
        with _registry_lock:
            if _jobs.get(corpus) is threading.current_thread():
                _jobs.pop(corpus, None)


def _execute(spec: GatherSpec) -> None:
    """Run the gather pipeline, writing status as each stage begins and on
    completion. Any exception is recorded as a failed status, not raised —
    the thread has no caller to surface it to."""
    # Imported lazily so the read-only serve path doesn't pull the gather
    # pipeline (and its many imports) unless gather is actually enabled.
    from . import __main__ as gather_main  # pylint: disable=import-outside-toplevel

    corpus = spec.corpus
    status: Dict[str, Any] = {
        "corpus": corpus,
        "state": "running",
        "spec": spec.to_dict(),
        "pid": os.getpid(),
        "started": _now_iso(),
        "updated": _now_iso(),
        "finished": None,
        "stage": None,
        "stage_index": 0,
        "stage_total": None,
        "error": None,
    }

    def _persist() -> None:
        status["updated"] = _now_iso()
        path = _status_path(corpus)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with atomic_open(path) as handle:
                json.dump(status, handle, indent=2, sort_keys=True)
        except OSError as err:
            log(
                f"gather_runner: could not write status for {corpus}: {err}",
                Verbosity.STATUS,
                level=LogLevel.ERROR,
            )

    def _progress(name: str, index: int, total: int) -> None:
        status["stage"] = name
        status["stage_index"] = index
        status["stage_total"] = total
        _persist()

    _persist()
    try:
        ok = gather_main.run_gather(
            spec.to_argv(), Verbosity.STATUS, progress=_progress
        )
        if ok:
            status["state"] = "done"
        else:
            status["state"] = "failed"
            status["error"] = (
                f"'{corpus}' is not a recognized Working Group / Research "
                "Group, a known mailing list, a draft/repo set, or a "
                "synthetic (x-) corpus. Check the spelling or add sources."
            )
    except BaseException as err:  # pylint: disable=broad-except
        status["state"] = "failed"
        status["error"] = f"{type(err).__name__}: {err}"[:500]
    finally:
        status["finished"] = _now_iso()
        _persist()


def read_status(corpus: str) -> Optional[Dict[str, Any]]:
    """Read one corpus's gather status, or None if none recorded.

    A `running` record whose recorded pid is no longer alive is relabelled
    `interrupted` — the gather thread is a daemon, so a server restart/kill
    leaves the status stuck at `running` forever otherwise. (The terminal
    `done`/`failed` status is written while still holding the gather lock,
    so a genuinely-running gather always has a live pid.)
    """
    try:
        with open(_status_path(corpus), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        result = dict(data)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if result.get("state") == "running":
        pid = result.get("pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            result["state"] = "interrupted"
    return result


def all_statuses() -> List[Dict[str, Any]]:
    """Every recorded gather status, newest activity first."""
    root = get_cache_dir()
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if name.startswith((".", "_")):
            continue
        status = read_status(name)
        if status is not None:
            out.append(status)
    out.sort(key=lambda s: str(s.get("updated") or ""), reverse=True)
    return out
