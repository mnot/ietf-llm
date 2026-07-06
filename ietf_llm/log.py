"""Terminal / stderr output for the CLIs and the MCP server.

`log()` writes verbosity-filtered status / progress / error messages to stderr —
human-readable with optional ANSI colour, or one-line structured JSON when
``IETF_LLM_LOG_FORMAT=json`` (for a cloud log collector). `Verbosity` /
`LogLevel` classify what shows. `graceful_keyboard_interrupt` wraps a CLI entry
point so Ctrl-C exits cleanly (status 130) instead of dumping a traceback.
Stdlib-only leaf.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional


def graceful_keyboard_interrupt(
    entry: "Callable[[], None]",
) -> "Callable[[], None]":
    """Decorator that wraps a CLI `main()` so Ctrl-C exits cleanly.

    A bare `ietf-llm` etc. would otherwise dump a KeyboardInterrupt
    traceback on Ctrl-C — ugly, and looks like a crash. The wrapped
    entry catches it, prints a one-line "Interrupted." to stderr, and
    exits with status 130 (the conventional "terminated by Ctrl-C" code).
    """

    def runner() -> None:
        try:
            entry()
        except KeyboardInterrupt:
            # Newline first because Ctrl-C usually lands mid-line.
            print("\nInterrupted.", file=sys.stderr)
            sys.exit(130)

    runner.__name__ = entry.__name__
    runner.__doc__ = entry.__doc__
    return runner


class Verbosity(Enum):
    """Logging verbosity settings."""

    QUIET = 0
    STATUS = 1
    VERBOSE = 2


class LogLevel(Enum):
    """Logging message levels."""

    ERROR = 0
    WARN = 1
    STATUS = 2
    PROGRESS = 3


# ANSI markers for the level prefix only — we never colour whole lines, just
# the bracketed tag. Applied solely when stderr is an interactive terminal and
# we're in text (not JSON) mode; honours the NO_COLOR convention.
_LEVEL_PREFIX = {LogLevel.ERROR: "[ERROR] ", LogLevel.WARN: "[WARN] "}
_LEVEL_COLOR = {LogLevel.ERROR: "\033[31m", LogLevel.WARN: "\033[33m"}
_ANSI_RESET = "\033[0m"


def _use_color() -> bool:
    """True when it's safe to emit ANSI colour on stderr: an interactive
    terminal, not the JSON log format, and NO_COLOR unset (https://no-color.org)."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("IETF_LLM_LOG_FORMAT", "").strip().lower() == "json":
        return False
    try:
        return sys.stderr.isatty()
    except (ValueError, AttributeError):
        return False


def log(
    message: str,
    verbosity: Verbosity = Verbosity.STATUS,
    level: LogLevel = LogLevel.PROGRESS,
    fields: Optional[Dict[str, Any]] = None,
) -> None:
    """Print a status / progress / error message to stderr.

    Everything `log()` emits is narration about what the tool is doing,
    not program output; writing to stderr keeps it clear of any stdout
    a caller might be piping (e.g. `ietf-llm-search` results) and, for the
    stdio MCP transport, stdout *is* the protocol, so logs must never go
    there. Convention matches curl, git, wget, etc.

    - level: LogLevel.ERROR / WARN / STATUS / PROGRESS — ERROR always shows;
      WARN and STATUS show unless --quiet; PROGRESS shows only under --verbose.
      On an interactive terminal the ERROR / WARN tag is coloured (red / yellow);
      see `_use_color`.
    - Set IETF_LLM_LOG_FORMAT=json for one-line structured JSON records
      (ts / level / msg) for the container deployment, where a log
      collector ingests them. Container runtimes capture stderr (and
      stdout is reserved for the stdio protocol), so structured logs go to
      stderr too. Messages carry no secrets -- keep it that way.
    - fields: extra structured key/values merged into the JSON record so a
      record stays queryable by field (e.g. a per-request access line
      carrying tool / status / duration_ms). Ignored in text mode, where
      the human-readable `message` already carries the summary; the fixed
      ts / level / msg keys always win over a same-named field. Keep these
      secret-free too.
    """
    if level == LogLevel.ERROR:
        visible = True
    elif verbosity == Verbosity.QUIET:
        visible = False
    elif verbosity == Verbosity.VERBOSE:
        visible = True
    else:  # Verbosity.STATUS
        visible = level in (LogLevel.WARN, LogLevel.STATUS)
    if not visible:
        return

    if os.environ.get("IETF_LLM_LOG_FORMAT", "").strip().lower() == "json":
        record = {
            **(fields or {}),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level.name.lower(),
            "msg": message,
        }
        print(json.dumps(record), file=sys.stderr)
        return

    prefix = _LEVEL_PREFIX.get(level, "")
    if prefix and _use_color():
        prefix = f"{_LEVEL_COLOR[level]}{prefix.rstrip()}{_ANSI_RESET} "
    print(f"{prefix}{message}", file=sys.stderr)
