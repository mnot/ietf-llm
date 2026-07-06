"""Live Datatracker fact lookups for the chair-workflow read tools.

This package is the one read-path exception to the server's offline rule.
`meeting_schedule` and `draft_status` hit Datatracker *live* because the
facts they serve — meeting schedules, IESG document states — change daily
and a gather cache (a 36-month window, often days stale) is too coarse for
an agenda. They are therefore gated exactly like the gather tools
(registered only when `freshness.gather_enabled()` is true: on for a local
stdio server, off for the shared read-only HTTP replica) and imported
lazily by `ietf_llm.mcp`, so the default read path never pulls this in.

Unlike the gather layer it keeps only a small TTL cache — in-process, plus a
best-effort cross-process copy on disk (`.live-cache.json` under the cache
root). The disk copy exists so a cold, short-lived process — one that starts
with an empty in-process cache, e.g. an MCP stdio subprocess restarted between
sessions — does not re-hit Datatracker on every call; without it a burst of
cold processes would hammer the API. It writes nothing else: no ETag store, and
never any corpus content, so a re-gather is still the only thing that mutates
a corpus. On a read-only cache mount the disk write silently no-ops. Every
result carries the UTC time the underlying data was fetched, so a caller can
see how fresh (or how stale-on-failure) it is; the tool wrappers render that
via `age_stamp`.

The data source is always the Datatracker REST API / agenda JSON, never a
scraped HTML page (see `docs/architecture.md`, "Use the Datatracker API").

Implementation is split across cohesive submodules:

  cache.py     — the TTL-cached fetch seam (`_fetch_json`), the in-process +
                 on-disk cache, shared base URLs / datetime helpers, age_stamp
  meetings.py  — a group's sessions at a meeting + its upcoming meetings
  drafts.py    — per-draft live status + overview reconciliation

Every externally-used symbol is re-exported here so callers can keep using
`live_lookup.<name>`. The one exception is the network seam `_fetch_json`:
it is *not* re-exported, so a test stubs it at its real home
(`live_lookup.cache._fetch_json`) — patching the package would silently miss
the intra-`cache` call sites.
"""

from __future__ import annotations

from .cache import (
    _cached_json,
    _disk_get,
    _live_cache_path,
    _reset_cache,
    age_stamp,
)
from .drafts import (
    DraftReconciliation,
    DraftStatus,
    _derive_eligibility,
    fetch_draft_status,
    reconcile_active_drafts,
)
from .meetings import (
    MeetingSession,
    UpcomingMeeting,
    fetch_meeting_sessions,
    fetch_upcoming_meetings,
    is_interim_number,
    meeting_id_label,
)

__all__ = [
    # Meetings
    "MeetingSession",
    "UpcomingMeeting",
    "fetch_meeting_sessions",
    "fetch_upcoming_meetings",
    "is_interim_number",
    "meeting_id_label",
    # Drafts
    "DraftStatus",
    "DraftReconciliation",
    "fetch_draft_status",
    "reconcile_active_drafts",
    # Cache / rendering
    "age_stamp",
    # Re-exported for tests
    "_cached_json",
    "_disk_get",
    "_live_cache_path",
    "_reset_cache",
    "_derive_eligibility",
]
