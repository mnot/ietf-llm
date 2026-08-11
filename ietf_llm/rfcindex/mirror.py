"""The RFC plain-text mirror the publisher joins the index against.

rfc.fyi's index carries byte ranges into each RFC's `.txt`, not the prose
itself, so recovering chunk text means holding the same bytes the build held.
This module maintains that mirror and, more importantly, proves it is the
same one.

**Why the proof is needed.** RFC 9920 §7.6 permits reissuing a published RFC
-- a deliberate change from RFC 9280's "once published, RFCs are not changed"
-- and §7.8 allows it "to maintain a consistent presentation". Presentation
is exactly what a byte offset keys on. So `(rfc, off, len)` is stable within
one publication version and not across a reissue, and the failure is the
quiet kind: a reissued RFC still has *a* chunk at that offset, the join still
succeeds, and one document's text is silently attached to another's vectors.

`reconcile` is the guard. Every published index carries a sha256 per source
file (`sources.json`), so a mirror can be compared against the exact bytes
the build saw, per RFC. An RFC that differs has its chunks dropped rather
than mis-joined, and the count is reported rather than swallowed -- a join
that quietly halves is a bug, and a join that quietly mis-attributes is
worse.

Publisher-side only -- see the subpackage docstring.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..log import LogLevel, Verbosity, log
from .format import RfcIndexError

#: The RFC Editor's canonical rsync endpoint for plain-text RFCs. rsync
#: rather than HTTPS because the mirror is ~530 MB and refreshed monthly:
#: an incremental sync transfers the handful of new files, where a bulk
#: re-fetch would move the whole series every time. Same source rfc.fyi's
#: own build uses, which is part of why the digests line up.
RSYNC_SOURCE = "ftp.rfc-editor.org::rfcs-text-only"

#: Only the numbered plain-text RFCs. The module keeps the same filter as the
#: build, so the mirror holds neither more nor less than the index describes.
_RSYNC_ARGS = ("-az", "--include=rfc[0-9]*.txt", "--exclude=*")

_READ_CHUNK = 1 << 20


def text_path(mirror_dir: str, rfc: str) -> str:
    """Path of one RFC's plain text within the mirror.

    `rfc` is the index's own identifier, which is a string because two chunks
    in the corpus carry `"17a"` — so this formats rather than arithmetics.
    """
    return os.path.join(mirror_dir, f"rfc{rfc}.txt")


def digest_file(path: str) -> str:
    """Streaming sha256 of a file, matching what `sources.json` records."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_READ_CHUNK)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


@dataclass
class Reconciliation:
    """Which RFCs in the mirror are the ones the index was built from."""

    matched: int = 0
    #: Present locally but not byte-identical — a reissue, or a mirror at a
    #: different moment. Their chunks cannot be joined.
    differing: List[str] = field(default_factory=list)
    #: In the index but not in the mirror at all.
    absent: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.matched + len(self.differing) + len(self.absent)

    @property
    def usable(self) -> bool:
        """True when every RFC the index describes is present and identical.

        Deliberately strict: a partial mirror still produces a usable corpus
        (the unmatched RFCs are simply dropped), so this is a signal for the
        caller to log or refuse on, not a precondition `join` enforces.
        """
        return not self.differing and not self.absent

    def summary(self) -> str:
        if not self.total:
            return "no per-RFC digests in this index — mirror unverified"
        parts = [f"{self.matched:,}/{self.total:,} RFCs match the build"]
        if self.differing:
            sample = ", ".join(self.differing[:5])
            parts.append(f"{len(self.differing):,} differ ({sample}…)")
        if self.absent:
            parts.append(f"{len(self.absent):,} absent")
        return "; ".join(parts)


def reconcile(mirror_dir: str, digests: Dict[str, str]) -> Reconciliation:
    """Compare `mirror_dir` against the index's per-RFC `sources.json` digests.

    An empty `digests` means the index predates the sidecar, which is a real
    case for an early release. The result is then empty rather than negative
    — nothing matched, nothing differed, `usable` True — because there was
    nothing to check against. `summary()` says so in words; a bare "0/0"
    reads as failure when the truth is "unverifiable".
    """
    result = Reconciliation()
    if not digests:
        return result
    for rfc, want in sorted(digests.items()):
        path = text_path(mirror_dir, rfc)
        if not os.path.isfile(path):
            result.absent.append(rfc)
        elif digest_file(path) == want:
            result.matched += 1
        else:
            result.differing.append(rfc)
    return result


def sync_mirror(
    mirror_dir: str,
    source: str = RSYNC_SOURCE,
    verbosity: Verbosity = Verbosity.STATUS,
    timeout: Optional[int] = 3600,
) -> None:
    """Bring `mirror_dir` up to date with the RFC Editor.

    No `--delete`: a withdrawn file is not a reason to lose a mirror that an
    older index still describes, and the join is keyed on digests anyway, so
    a stale extra file is inert. Raises `RfcIndexError` if rsync is missing
    or fails — this is the publisher's input, and continuing with a partial
    mirror would silently shrink the corpus.
    """
    binary = shutil.which("rsync")
    if binary is None:
        raise RfcIndexError(
            "rsync is not on PATH; it is how the RFC text mirror is maintained"
        )
    os.makedirs(mirror_dir, exist_ok=True)
    argv = [binary, *_RSYNC_ARGS, f"{source}/", mirror_dir + os.sep]
    log(f"syncing RFC text from {source}", verbosity, LogLevel.PROGRESS)
    try:
        proc = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise RfcIndexError(f"rsync from {source} failed: {err}") from err
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise RfcIndexError(
            f"rsync from {source} exited {proc.returncode}"
            + (f": {detail[-1]}" if detail else "")
        )
