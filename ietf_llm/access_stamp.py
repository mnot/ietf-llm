"""Read-path access stamping.

When a reader resolves a corpus through the MCP read tools, we record that the
corpus was used, so a refresh cron (`ietf-llm --all --used-within N`) can
re-gather only corpora that are actually being read and let unused ones go
stale — keeping a deployment fresh without perpetuating zombie corpora.

The durable write goes through the corpus store (`record_access`): a
`last-accessed` sentinel on the local backend, the `access` control-plane key on
the cloud backend. This module sits in front of it with two jobs:

  - **Coarsening.** Downstream granularity is days, so we stamp a corpus at
    most once per `STAMP_MIN_INTERVAL_SECONDS` *per process*, tracked in memory.
    A long-lived serve process therefore issues ~one write per corpus per
    window regardless of read volume — the per-read cost is a dict lookup, not
    a store round-trip.

  - **Best-effort.** The store write is wrapped so a failure (e.g. a read-only
    IAM role on a locked-down cloud serve fleet, or a read-only index mount
    locally) never propagates into a read. `IETF_LLM_RECORD_ACCESS=off` opts
    out entirely, for a deployment that wants the read path to make zero writes.

This is the only write the read path makes, and it touches no version, blob, or
pointer — see `docs/storage.md`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict

from .store.corpus import get_corpus_store

_RECORD_ACCESS_ENV = "IETF_LLM_RECORD_ACCESS"
_FALSY = ("0", "false", "no", "off")

#: Per-process debounce window: stamp a corpus at most once per this many
#: seconds. Coarse on purpose — the only consumer filters by whole days.
STAMP_MIN_INTERVAL_SECONDS = 6 * 3600.0

#: corpus -> monotonic time of its last stamp in this process.
_last_stamped: Dict[str, float] = {}
_lock = threading.Lock()


def record_access_enabled() -> bool:
    """False only when `IETF_LLM_RECORD_ACCESS` is explicitly falsy; default
    on. The opt-out for a deployment that wants a zero-write read path."""
    return os.environ.get(_RECORD_ACCESS_ENV, "").strip().lower() not in _FALSY


def note_access(corpus: str) -> None:
    """Record that `corpus` was just read, coarsened and best-effort.

    Returns immediately when stamping is disabled or when this process already
    stamped `corpus` within the debounce window; otherwise records the access
    through the corpus store, swallowing any failure so a read never fails on
    the stamp write.
    """
    if not record_access_enabled():
        return
    now = time.monotonic()
    with _lock:
        last = _last_stamped.get(corpus)
        if last is not None and now - last < STAMP_MIN_INTERVAL_SECONDS:
            return
        # Record the attempt before the write: even if the store write fails we
        # don't want to retry it on every subsequent read for the next window.
        _last_stamped[corpus] = now
    try:
        get_corpus_store().record_access(corpus)
    except Exception:  # pylint: disable=broad-except
        pass


def _reset_for_test() -> None:
    """Clear the in-process debounce state (test seam)."""
    with _lock:
        _last_stamped.clear()
