"""Concurrency-safe filesystem primitives.

Atomic writes (`atomic_open` / `atomic_open_binary` / `write_if_changed`) so a
reader — an MCP tool reading the corpus while a gather runs — never sees a
partial file; and best-effort cross-process advisory locks (`file_lock` /
`lock_is_held`) to serialise access to a shared resource across concurrent
gathers. Stdlib-only; sits at the bottom of the import graph (imports nothing
from the rest of the package).
"""

from __future__ import annotations

import itertools
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX (e.g. Windows)
    _fcntl = None  # type: ignore[assignment]


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
