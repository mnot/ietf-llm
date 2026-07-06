# pylint: disable=invalid-name
"""Temporary debug telemetry for diagnosing MCP client stalls/timeouts.

Writes JSONL events to
    <cache>/_debug/mcp-<pid>-<startISO>.jsonl

One file per server process so concurrent sessions never collide and the
filename identifies the run. The MCP tool `get_session_log` reads the
tail of the current process's file and returns it to the client so a
running session can be inspected from the LLM side.

This module is intentionally outside the documented `ietf_llm/` surface:
the server is normally read-only and never writes to disk; this facility
violates that boundary deliberately so it stays decoupled from the MCP
event loop (a stalled loop can't help us write its own diagnostics).

Opt-in via `IETF_LLM_DEBUG_LOG`. Default is off — set to "1" (or
"true"/"yes"/"on") in the MCP server's launch env to enable for a
session. Most MCP clients (Claude Desktop, Claude Code, Cursor, Zed)
let you set env vars per-server in their config.

Events emitted per request:
    offload_start    — _offload entered (tool dispatched off the loop)
    thread_started   — worker thread picked up the call
                       (gap from offload_start = anyio pool queue wait)
    thread_returned  — tool function returned cleanly
    thread_error     — tool function raised
    offload_end      — _offload about to return (always emitted via finally)

Plus periodic `heartbeat` events from a daemon thread so an idle
process is distinguishable from a wedged one, and a `session_start`
record at init.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from ..paths import get_cache_dir

_counter = itertools.count(1)
_calls_seen = 0  # snapshot for heartbeat (itertools.count has no peek)
_calls_lock = threading.Lock()
_start_monotonic = time.monotonic()
_fd: Optional[int] = None
_path: Optional[Path] = None
_enabled = False
_heartbeat_thread: Optional[threading.Thread] = None

_TRUTHY = {"1", "true", "yes", "on"}


def _is_enabled_env() -> bool:
    return os.environ.get("IETF_LLM_DEBUG_LOG", "").strip().lower() in _TRUTHY


def init() -> None:
    """Create the per-process log file and start the heartbeat thread.

    Idempotent; safe to call multiple times. If the cache dir isn't
    writable, logging silently disables itself rather than crashing the
    server."""
    # pylint: disable=global-statement
    global _fd, _path, _enabled, _heartbeat_thread
    if _enabled:
        return
    if not _is_enabled_env():
        return
    try:
        base = Path(get_cache_dir()) / "_debug"
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _path = base / f"mcp-{os.getpid()}-{stamp}.jsonl"
        _fd = os.open(str(_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        _enabled = True
    except OSError:
        _enabled = False
        return
    log_event(
        0,
        "session_start",
        pid=os.getpid(),
        python=sys.version.split()[0],
        path=str(_path),
        argv=sys.argv,
    )
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="ietf-llm-debug-heartbeat", daemon=True
    )
    _heartbeat_thread.start()


def next_id() -> int:
    """Monotonic request id; correlates the per-call events."""
    # pylint: disable=global-statement
    global _calls_seen
    with _calls_lock:
        _calls_seen += 1
    return next(_counter)


def log_event(req_id: int, event: str, **fields: Any) -> None:
    """Append one JSONL record. Cheap, lock-free (relies on O_APPEND
    atomicity for small writes on POSIX). Errors are swallowed —
    debug telemetry must never break the server."""
    if not _enabled or _fd is None:
        return
    record = {
        "t": round(time.monotonic() - _start_monotonic, 6),
        "wall": datetime.now(timezone.utc).isoformat(),
        "id": req_id,
        "event": event,
        **fields,
    }
    try:
        line = (json.dumps(record, default=str, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        os.write(_fd, line)
    except (OSError, TypeError, ValueError):
        pass


def _heartbeat_loop() -> None:
    """Emit a heartbeat every 10s from a daemon thread.

    Runs off the asyncio event loop so an event-loop stall doesn't stop
    heartbeats — a gap in `heartbeat` events therefore implicates the
    *process* (paused, swapped out, killed), while heartbeats continuing
    through a long quiet stretch confirm the process is alive and the
    issue is upstream (stdin not delivering, client not sending)."""
    # Lazy import: the transport module is independent of debug
    # logging, so don't pull it into the import graph unless we're
    # actually going to call its getter.
    # pylint: disable=import-outside-toplevel
    from . import stdio

    while _enabled:
        time.sleep(10)
        with _calls_lock:
            seen = _calls_seen
        log_event(
            0,
            "heartbeat",
            calls_seen=seen,
            uptime=round(time.monotonic() - _start_monotonic, 3),
            writer_queue=stdio.queue_state(),
        )


def read_tail(
    limit: int = 200, since_seconds: Optional[float] = None
) -> List[dict[str, Any]]:
    """Read recent events from the current process's log file.

    Whole-file read + slice — fine for a temporary debug log; sessions
    rarely exceed a few thousand events. Returns [] if logging is off
    or the file isn't there yet."""
    if _path is None or not _path.exists():
        return []
    events: List[dict[str, Any]] = []
    try:
        with open(_path, "rb") as handle:
            for raw in handle:
                try:
                    events.append(json.loads(raw))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except OSError:
        return []
    if since_seconds is not None:
        cutoff = (time.monotonic() - _start_monotonic) - since_seconds
        events = [e for e in events if e.get("t", 0) >= cutoff]
    if limit > 0:
        events = events[-limit:]
    return events


def current_path() -> Optional[str]:
    return str(_path) if _path else None


def is_enabled() -> bool:
    return _enabled
