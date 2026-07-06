import itertools
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    _fcntl = None  # type: ignore[assignment]

from . import __version__

DEFAULT_MONTHS = 12


def months_request_error(months: Optional[int], force: bool) -> Optional[str]:
    """Validate a requested `--months` window, returning a refusal message or
    None.

    `months=0` means *all history* — an unbounded gather that, on an active
    list, pulls tens of thousands of messages over IMAP and can run for a very
    long time (and, on an ephemeral host, may be lost if it is recycled before
    it publishes). So it is honoured only with `force`, to keep it from being
    selected by accident (e.g. a caller meaning "minimal" passing 0). A negative
    window is nonsensical. A bounded window (>=1) — or an unset `None` that later
    defaults to `DEFAULT_MONTHS` — is always allowed."""
    if months is None:
        return None
    if months < 0:
        return (
            f"months must be 0 or a positive number (got {months}): 0 means all "
            "history, a positive number is a month window."
        )
    if months == 0 and not force:
        return (
            "months=0 fetches the entire list history — on an active list that "
            "is tens of thousands of messages over IMAP and can take a very long "
            "time. Pass a bounded window instead (e.g. 12), or force to confirm "
            "you really want all of it."
        )
    return None


def months_request_caution(months: Optional[int]) -> Optional[str]:
    """A non-blocking heads-up for a large but bounded window, or None. Bounded
    windows are always allowed; this just flags that one well past the default
    will be slower. The unbounded `0` case is handled by `months_request_error`."""
    if months is not None and months > DEFAULT_MONTHS:
        return (
            f"Note: a {months}-month window is well past the {DEFAULT_MONTHS}-"
            "month default; on an active list expect a longer gather — poll "
            "gather_status to watch it."
        )
    return None


def resolve_months(months: Optional[int], force: bool) -> Tuple[int, Optional[str]]:
    """Resolve the effective month window after config has been merged, returning
    `(window, note)`. all-history (`months=0`) is a per-invocation choice, not a
    sticky setting: it applies only with `force`, so a *stored* 0 on an unforced
    run degrades to `DEFAULT_MONTHS` (with an explanatory `note`) rather than
    silently making every refresh unbounded. `None` resolves to the default."""
    if months == 0 and not force:
        return DEFAULT_MONTHS, (
            f"stored months=0 (all history) applies only with --force; using the "
            f"default {DEFAULT_MONTHS}-month window"
        )
    return (DEFAULT_MONTHS if months is None else months), None


def cached_wg_names() -> List[str]:
    """Shortnames of every gathered WG / corpus — directories with a
    `files/` subdir under the cache root, sorted. Skips dot- and
    underscore-prefixed entries (machinery like `_github-users.json`).

    Shared by `ietf-llm --all` / `--list` and the shell-completion
    completer for the `wg` positional, so they can't drift.
    """
    root = get_cache_dir()
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name.startswith("_"):
            continue
        if os.path.isdir(os.path.join(root, name, "files")):
            out.append(name)
    return out


def wg_completer(prefix: str, **_kwargs: Any) -> List[str]:
    """argcomplete completer for a `wg` positional: cached shortnames
    matching `prefix`. Keep it fast — argcomplete spins a fresh
    interpreter per <TAB>, so this is just a directory listing.
    """
    return [w for w in cached_wg_names() if w.startswith(prefix)]


def maybe_autocomplete(parser: Any) -> None:
    """Wire argcomplete into `parser` if the package is installed.

    Called right before `parse_args()`. A no-op (not an error) when
    argcomplete isn't present, so a minimal / editable install
    without the dependency still runs the CLI normally.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    argcomplete.autocomplete(parser)


def print_completion_snippet(shell: str) -> int:
    """Print the argcomplete registration snippet for every ietf-llm
    command, for the given shell. Returns an exit code.

    Routed through `ietf-llm` itself (not argcomplete's own
    `register-python-argcomplete` script) because under `pipx` only
    this package's declared entry points are on PATH — a dependency's
    scripts aren't exposed. `eval "$(ietf-llm --completion zsh)"`
    works regardless of how the package was installed.
    """
    try:
        import argcomplete  # pylint: disable=import-outside-toplevel
    except ImportError:
        print(
            "argcomplete is not installed (it ships with ietf-llm; "
            "try reinstalling).",
            file=sys.stderr,
        )
        return 1
    commands = ["ietf-llm", "ietf-llm-export", "ietf-llm-search"]
    # argcomplete ships no type stubs; shellcode isn't in its __all__.
    snippet = argcomplete.shellcode(  # type: ignore[attr-defined,no-untyped-call]
        commands,
        shell=shell,
    )
    print(snippet)
    return 0


def is_synthetic_wg(name: str) -> bool:
    """True for synthetic / non-WG corpora (the `x-` prefix convention).

    Some collections of drafts and mailing lists predate (or sit
    parallel to) any formal WG, but it's still useful to gather them
    into the same corpus shape so the MCP server and search tools
    can answer questions about them. The `x-` prefix opts out of
    every Datatracker / WG-page lookup (no charter, no leadership,
    no auto-discovered drafts or mailing list, no transcripts) while
    leaving everything else — mail thread reconstruction, GitHub
    issue gathering, the explicit `--draft` / `--mailing-list`
    additions, indexing — working as normal.

    Naming convention chosen for brevity and zero risk of colliding
    with a real IETF WG shortname (none start with `x-`).
    """
    return name.startswith("x-")


def get_config_dir() -> str:
    """Return the configuration directory, creating it if necessary.

    Honours ``IETF_LLM_CONFIG_DIR`` (env > default) so a deployment can
    point per-WG config at a mounted location; defaults to
    ``~/.config/ietf-llm`` for the local CLI.
    """
    config_dir = os.environ.get("IETF_LLM_CONFIG_DIR", "").strip()
    if not config_dir:
        config_dir = os.path.expanduser("~/.config/ietf-llm")
    if not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    return config_dir


def get_cache_dir() -> str:
    """Return the cache directory, creating it if necessary.

    Honours ``IETF_LLM_CACHE_DIR`` (env > default) so a deployment can
    point the corpus root at the synced / mounted location; defaults to
    ``~/.cache/ietf-llm`` for the local CLI. The tree is relocatable
    (chunk paths are relative to the cache root), so an absolute override
    here moves the whole corpus.
    """
    cache_dir = os.environ.get("IETF_LLM_CACHE_DIR", "").strip()
    if not cache_dir:
        cache_dir = os.path.expanduser("~/.cache/ietf-llm")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_index_dir() -> str:
    """Return the directory tree holding per-WG embedding index databases.

    Honours ``IETF_LLM_INDEX_DIR`` (env > default) so a deployment can put
    the hot, frequently-read ``<wg>/embeddings.db`` files on fast or
    RAM-backed storage (tmpfs) separately from the corpus files. Defaults
    to the cache root, so the local layout
    (``<cache>/<wg>/embeddings.db``) is unchanged.
    """
    index_dir = os.environ.get("IETF_LLM_INDEX_DIR", "").strip()
    if not index_dir:
        index_dir = get_cache_dir()
    if not os.path.exists(index_dir):
        os.makedirs(index_dir, exist_ok=True)
    return index_dir


def get_wg_file_cache_dir(wg_name: str) -> str:
    """Get the local file cache directory for a Working Group."""
    cache_dir = os.path.join(get_cache_dir(), wg_name, "files")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


class LockHeld(Exception):
    """Raised by `file_lock(..., blocking=False)` when the lock is already
    held by another owner (process or, with separate handles, this one)."""


@contextmanager
def file_lock(lock_path: str, blocking: bool = True) -> "Iterator[None]":
    """Best-effort cross-process exclusive lock (flock) held for the
    `with` body. Used to serialise access to a shared resource across
    concurrent gathers — notably the single transcripts git clone, where
    two simultaneous clone/pull operations would collide on git's
    index.lock and corrupt the tree.

    With `blocking=False` the lock is taken non-blocking (LOCK_NB): if
    another holder has it, `LockHeld` is raised instead of waiting. The
    MCP gather runner uses this to answer "is a gather of this corpus
    already running?" without stalling.

    A no-op where `fcntl` is unavailable (non-POSIX); the lock file
    itself is just a handle and is left in place between runs.
    """
    if _fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        if blocking:
            _fcntl.flock(handle, _fcntl.LOCK_EX)
        else:
            try:
                _fcntl.flock(handle, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError as err:
                raise LockHeld(lock_path) from err
        try:
            yield
        finally:
            _fcntl.flock(handle, _fcntl.LOCK_UN)


def lock_is_held(lock_path: str) -> "Optional[bool]":
    """Non-blocking probe of a `file_lock`: True if currently held by some
    owner, False if free, None if undeterminable.

    This is the authoritative liveness signal for a resource guarded by
    `file_lock` — a held flock is released by the OS the instant its holder
    dies, and (unlike a recorded pid) it is meaningful across hosts sharing
    the cache filesystem and immune to pid reuse. Opens the lock file
    read-only so it works on a read-only mount, and returns None rather than
    guessing when it cannot tell: `fcntl` unavailable (non-POSIX), or the
    file cannot be opened. (Reliability still depends on the filesystem's
    flock support — a guess-free None on exotic mounts is the honest answer.)
    """
    if _fcntl is None:
        return None
    if not os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, "r", encoding="utf-8") as handle:
            try:
                _fcntl.flock(handle, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                return True
            _fcntl.flock(handle, _fcntl.LOCK_UN)
            return False
    except OSError:
        return None


#: Per-process counter making each `atomic_open` temp name unique even across
#: threads writing the same path; `next()` is atomic under the GIL.
_atomic_tmp_counter = itertools.count()


@contextmanager
def atomic_open(
    path: str, encoding: str = "utf-8", newline: Optional[str] = None
) -> "Iterator[Any]":
    """Open a text file for writing such that readers never see a
    partial result: writes go to a temp file in the same directory and
    are `os.replace`d into place (atomic on POSIX) only on clean close.

    The temp name carries the pid *and* a per-process counter so concurrent
    writers — two processes, or two threads of one process writing the same
    path (e.g. the gather worker and an enqueue both touching a status file) —
    never share a temp and clobber each other's rename. On error the temp is
    removed and the original left intact. Load-bearing for the
    MCP-reads-during-gather case.

    `newline` is passed through to `open` — `write_if_changed` uses
    `"\n"` so the bytes written match its LF-normalised comparison.
    """
    tmp = f"{path}.{os.getpid()}.{next(_atomic_tmp_counter)}.tmp"
    handle = open(  # pylint: disable=consider-using-with
        tmp, "w", encoding=encoding, newline=newline
    )
    try:
        yield handle
        handle.close()
        os.replace(tmp, path)
    except BaseException:
        handle.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


@contextmanager
def atomic_open_binary(path: str) -> "Iterator[Any]":
    """Binary counterpart of `atomic_open`: yields a file handle opened for
    `wb` writing into a same-directory temp that is `os.replace`d into place
    only on clean close. On error (including a download that raises partway)
    the temp is removed and any prior file is left intact — so a crash or a
    truncated transfer never leaves a partial blob cached under the final name.
    """
    tmp = f"{path}.{os.getpid()}.{next(_atomic_tmp_counter)}.tmp"
    handle = open(tmp, "wb")  # pylint: disable=consider-using-with
    try:
        yield handle
        handle.close()
        os.replace(tmp, path)
    except BaseException:
        handle.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_if_changed(path: str, content: str) -> bool:
    """Write `content` to `path` only if it differs from what's there
    (or the file is missing). Returns True if a write happened.

    The write is atomic (temp + rename), so a concurrent reader — an
    MCP tool reading the corpus while a gather runs — sees either the
    old bytes or the new, never a truncated file.

    Still useful to the incremental embedder, though no longer load-
    bearing for it: the embedder keys its skip on each file's content
    hash, so a byte-identical re-render is a no-op for embedding whether
    or not it rewrites. The per-thread / per-issue writers regenerate
    every file each gather; this guard avoids the needless rewrite (and
    the mtime churn other consumers may watch) when the render is
    unchanged.

    Line endings are normalised to LF. Source data carries CRLF (GitHub
    comment bodies, RFC 5322 mail) which would otherwise re-trigger a
    write every gather: the file is stored with CRLF, but reading it
    back in text mode translates CRLF→LF (universal newlines), so a
    naive `read() == content` never matched and the file churned
    forever. Normalising both the stored bytes and the comparison to LF
    fixes that and keeps the corpus single-newline.
    """
    normalised = content.replace("\r\n", "\n").replace("\r", "\n")
    data = normalised.encode("utf-8")
    try:
        with open(path, "rb") as fh:
            if fh.read() == data:
                return False
    except OSError:
        pass  # missing / unreadable → fall through and write
    with atomic_open(path, newline="\n") as fh:
        fh.write(normalised)
    return True


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


def format_filename(name: str) -> str:
    """Format a string to be a safe filename."""
    return re.sub(r"[^\w\s-]", "", name).strip().lower().replace(" ", "_")
