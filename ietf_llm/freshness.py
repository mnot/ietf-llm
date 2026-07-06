"""Per-WG cache freshness tracking.

Two per-corpus sentinel files under `~/.cache/ietf-llm/<wg>/`, each an
ISO 8601 UTC timestamp:

  - `last-gathered` — the most recent successful gather. Written by
    `gather.sequencer._gather_one`; read by `cli.export` (stale-cache warning) and
    `ietf_llm.mcp` (one-line freshness note on top-level tool responses).
  - `last-accessed` — the most recent read-path use of the corpus. Written
    (coarsely) by `access.note_access` off the MCP read chokepoint; read by
    the `--all --used-within` refresh filter so a cron re-gathers only
    corpora that are actually being used.

We deliberately don't warn when `last-gathered` is *missing* (the cache
predates this feature, or the user populated it some other way) —
forcing a re-gather to populate the sentinel would be noisier than
useful. One real gather and we're tracking it from there on.

The `local`-backend `CorpusStore` reaches these through `record_access` /
`last_accessed` / `last_gathered`; the `cloud` backend keeps the same
information in its control plane instead (a `last-accessed` sentinel on
ephemeral scratch would not survive). `iso_now` / `parse_iso` are the shared
timestamp format both backends use.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .paths import get_cache_dir

#: Cache older than this prompts the warning. Picked to be long enough
#: that day-to-day reading doesn't constantly nag, but short enough that
#: month-old material gets flagged before a user trusts it.
STALE_AFTER_DAYS = 7

#: Default freshness-debounce window for re-gathers, in hours. A re-gather
#: of a corpus last gathered within this window is skipped at the gather
#: entry point (the CLI and `start_gather`) unless forced. On a shared
#: server this collapses several clients' near-simultaneous "looks stale,
#: refresh it" decisions into one gather; locally it turns an accidental
#: immediate re-run into a no-op. Override with `IETF_LLM_GATHER_MIN_INTERVAL`
#: (float hours; 0 disables the debounce).
GATHER_MIN_INTERVAL_DEFAULT_HOURS = 6.0

_GATHERED_SENTINEL = "last-gathered"
_ACCESSED_SENTINEL = "last-accessed"
_MIN_INTERVAL_ENV = "IETF_LLM_GATHER_MIN_INTERVAL"


#: Fallback for `gather_enabled()` when `IETF_LLM_ENABLE_GATHER` is unset.
#: The MCP server sets this once at startup from the resolved transport
#: (stdio -> True, http -> False, via `set_gather_default`); every other
#: context (the CLI's freshness hints, import time) leaves it at the
#: conservative off. Kept here, beside `gather_enabled`, so the tool
#: registration gate and the user-facing "go gather" hints read one
#: resolved value and can't drift apart.
_GATHER_DEFAULT = False

_GATHER_TRUTHY = ("1", "true", "yes", "on")
_GATHER_FALSY = ("0", "false", "no", "off")


def set_gather_default(enabled: bool) -> None:
    """Set the fallback `gather_enabled()` uses when `IETF_LLM_ENABLE_GATHER`
    is unset.

    Called once at MCP-server startup with the resolved transport so a local
    stdio server defaults in-session gather on and a shared HTTP server
    defaults it off. An explicit env value still overrides either way, so
    this only moves the *default* — it is not a second on-switch.
    """
    global _GATHER_DEFAULT  # pylint: disable=global-statement
    _GATHER_DEFAULT = enabled


#: The MCP transport this server runs, set once at startup via
#: `set_deployment_mode`: 'stdio' (local, single-user) or 'http' (possibly
#: shared / hosted). Defaults to 'stdio' — the local single-user case, and the
#: one that must NOT trigger shared-cost hedging; the HTTP entry point sets it
#: explicitly. Read so the server can state its topology authoritatively, rather
#: than leaving a client to infer it from transport-flavoured tool wording.
_DEPLOYMENT_MODE = "stdio"


def set_deployment_mode(mode: str) -> None:
    """Record the resolved MCP transport ('stdio' or 'http') at server startup,
    so tool output can state whether the deployment is local single-user or
    possibly shared (which is what scopes the 'a wide gather costs everyone'
    cautions). Anything but 'http' normalises to 'stdio'."""
    global _DEPLOYMENT_MODE  # pylint: disable=global-statement
    _DEPLOYMENT_MODE = "http" if mode == "http" else "stdio"


def deployment_mode() -> str:
    """The resolved MCP transport: 'stdio' (local, single-user) or 'http'."""
    return _DEPLOYMENT_MODE


def _gather_env_override() -> Optional[bool]:
    """The explicit `IETF_LLM_ENABLE_GATHER` setting as a bool, or None when
    unset (or unrecognised) so the caller falls back to `_GATHER_DEFAULT`."""
    raw = os.environ.get("IETF_LLM_ENABLE_GATHER", "").strip().lower()
    if raw in _GATHER_TRUTHY:
        return True
    if raw in _GATHER_FALSY:
        return False
    return None


def gather_enabled() -> bool:
    """True when in-session gather (the `start_gather` MCP tool) is enabled.

    `IETF_LLM_ENABLE_GATHER` is an explicit override in *both* directions: a
    truthy value (`1`/`true`/`yes`/`on`) forces gather on, a falsy value
    (`0`/`false`/`no`/`off`) forces it off. When unset (or unrecognised) the
    result is `_GATHER_DEFAULT` — which the MCP server sets from the transport
    (stdio on, http off) via `set_gather_default`, and which every other
    context leaves at the conservative off.

    Lives here, not only in `ietf_llm.mcp`, so any module that emits a
    "go gather this" hint can name the gather path the caller actually has:
    an MCP client can call `start_gather` but cannot run a shell command, so
    pointing it at `ietf-llm` is a dead end. `mcp.common._gather_enabled`
    delegates here to keep one source of truth.
    """
    override = _gather_env_override()
    return _GATHER_DEFAULT if override is None else override


def gather_suggestion(corpus: str, *, purpose: str = "", force: bool = False) -> str:
    """One imperative clause telling the caller how to (re-)gather `corpus`,
    phrased for the environment.

    With in-session gather enabled it names the `start_gather` tool;
    otherwise the `ietf-llm <corpus>` shell command. `purpose` is an
    optional trailing clause (`"to refresh"`, `"to gather it"`); `force=True`
    adds the `force` argument for a deliberate re-gather of an already-cached
    corpus (no-op on the shell form, where the bare command re-gathers).
    """
    if gather_enabled():
        arg = ", force=True" if force else ""
        cmd = f'call `start_gather(corpus="{corpus}"{arg})`'
    else:
        cmd = f"run `ietf-llm {corpus}`"
    return f"{cmd} {purpose}".rstrip() if purpose else cmd


def _sentinel_path(wg: str, name: str = _GATHERED_SENTINEL) -> str:
    return os.path.join(get_cache_dir(), wg, name)


def iso_now() -> str:
    """ISO 8601 UTC string with a trailing Z, matching chunk_date format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(raw: str) -> Optional[datetime]:
    """Parse an `iso_now`-style timestamp to a tz-aware UTC datetime, or None
    if blank / malformed. Used by the local sentinels and by the cloud control
    plane, which stores the same string under its `access` key."""
    if not raw:
        return None
    try:
        # Python's fromisoformat accepts the trailing-Z form on 3.11+;
        # normalise older variants by rewriting to +00:00.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _write_sentinel(wg: str, name: str) -> None:
    """Stamp `wg`'s `name` sentinel with the current UTC time.

    Best-effort: if the WG cache directory doesn't exist yet, or the
    write fails (e.g. a read-only index mount), we don't raise. Freshness
    tracking is a UX/operability hint, not a correctness invariant; a failed
    write just means the next consumer falls back to the "unknown" path.
    """
    path = _sentinel_path(wg, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(iso_now())
    except OSError:
        pass


def _read_sentinel(wg: str, name: str) -> Optional[datetime]:
    """Read `wg`'s `name` sentinel as a tz-aware UTC datetime, or None if
    missing / unreadable / malformed."""
    path = _sentinel_path(wg, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    return parse_iso(raw)


def record_gather(wg: str) -> None:
    """Stamp the WG's `last-gathered` sentinel (best-effort; see
    `_write_sentinel`)."""
    _write_sentinel(wg, _GATHERED_SENTINEL)


def last_gathered(wg: str) -> Optional[datetime]:
    """The WG's last-gathered time, or None if unrecorded / unreadable."""
    return _read_sentinel(wg, _GATHERED_SENTINEL)


def record_access(wg: str) -> None:
    """Stamp the WG's `last-accessed` sentinel (best-effort; see
    `_write_sentinel`). The `local` backend's read-path access marker; the
    cloud backend records the same thing in its control plane instead."""
    _write_sentinel(wg, _ACCESSED_SENTINEL)


def last_accessed(wg: str) -> Optional[datetime]:
    """The WG's last read-path access time, or None if unrecorded /
    unreadable."""
    return _read_sentinel(wg, _ACCESSED_SENTINEL)


def _humanize_age(age_days: int) -> str:
    """`today` / `1 day ago` / `N days ago`."""
    if age_days <= 0:
        return "today"
    if age_days == 1:
        return "1 day ago"
    return f"{age_days} days ago"


def _stale_warning(wg: str, age_days: int, date: str) -> str:
    """The escalated, stale-cache warning line (with refresh prompt)."""
    return (
        f"⚠ {wg} cache last gathered {age_days} days ago "
        f"({date}); {gather_suggestion(wg, purpose='to refresh')}."
    )


def staleness_warning(wg: str, threshold_days: int = STALE_AFTER_DAYS) -> Optional[str]:
    """Return a one-line warning if the cache is older than the threshold.

    Returns None when the cache is fresh, or when we have no record of
    when it was last gathered (see module docstring for why we don't
    warn on absence). Single line — callers prepend or print as-is. Used
    by the export CLI, which only wants to speak up when something is
    actually stale.
    """
    when = last_gathered(wg)
    if when is None:
        return None
    age_days = (datetime.now(timezone.utc) - when).days
    if age_days < threshold_days:
        return None
    return _stale_warning(wg, age_days, when.strftime("%Y-%m-%d"))


def freshness_line(wg: str, threshold_days: int = STALE_AFTER_DAYS) -> Optional[str]:
    """A one-line freshness note for *every* top-level tool response.

    Always reports when the corpus was gathered, so a consumer knows the
    floor on their view (a question like "what happened today" is shaped
    by it). Escalates to the `staleness_warning` refresh prompt once the
    cache is older than `threshold_days`. Returns None only when there is
    no record of the gather time (absence is silent — see module docstring).
    """
    when = last_gathered(wg)
    if when is None:
        return None
    age_days = (datetime.now(timezone.utc) - when).days
    date = when.strftime("%Y-%m-%d")
    if age_days >= threshold_days:
        return _stale_warning(wg, age_days, date)
    return f"_{wg} corpus gathered {date} ({_humanize_age(age_days)})._"


def gather_min_interval_hours() -> float:
    """Resolve the gather debounce window (in hours) from the environment.

    `IETF_LLM_GATHER_MIN_INTERVAL` overrides `GATHER_MIN_INTERVAL_DEFAULT_HOURS`:
    a non-negative float, in hours, where 0 disables the debounce. A
    malformed or negative value falls back to the default rather than
    failing — the debounce is a guard, not something worth aborting a
    gather over.
    """
    raw = os.environ.get(_MIN_INTERVAL_ENV, "").strip()
    if not raw:
        return GATHER_MIN_INTERVAL_DEFAULT_HOURS
    try:
        hours = float(raw)
    except ValueError:
        return GATHER_MIN_INTERVAL_DEFAULT_HOURS
    return hours if hours >= 0 else GATHER_MIN_INTERVAL_DEFAULT_HOURS


def _humanize_delta(delta: timedelta) -> str:
    """Compact `Nh Nm` / `Nh` / `Nm` / `<1m` for a positive duration."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "<1m"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _format_hours(hours: float) -> str:
    """`6h` / `0.5h` — trim a trailing `.0` from a whole-number window."""
    return f"{hours:g}h"


def debounce_reason(
    wg: str, *, min_interval_hours: Optional[float] = None
) -> Optional[str]:
    """One-line 'fresh, skipped' note if `wg` was gathered within the
    debounce window, else None.

    Returns None — i.e. *go ahead and gather* — when there is no sentinel
    (a first gather is never debounced), when the window is disabled
    (`<= 0`), or when the cache is already older than the window.
    `--force` / `force=True` bypass the debounce by simply not calling
    this. A returned string is a success state, not an error: the existing
    snapshot is fresh enough to query as-is.
    """
    hours = (
        gather_min_interval_hours()
        if min_interval_hours is None
        else min_interval_hours
    )
    if hours <= 0:
        return None
    when = last_gathered(wg)
    if when is None:
        return None
    age = datetime.now(timezone.utc) - when
    if age >= timedelta(hours=hours):
        return None
    return (
        f"{wg} was gathered {_humanize_delta(age)} ago "
        f"({when.strftime('%Y-%m-%d %H:%M UTC')}), within the "
        f"{_format_hours(hours)} freshness window — skipped. Query the "
        "existing snapshot, or force a re-gather to override."
    )


#: CLI gather flags whose presence makes a run more than a plain refresh —
#: a source/scope change, or an explicit rebuild — so it bypasses the
#: debounce. `--months` is intentionally excluded: it has a default and a
#: bare re-run still carries it, so it can't signal intent here.
_SOURCE_CHANGE_FLAGS = (
    "draft",
    "mailing_list",
    "github",
    "github_label",
    "exclude_github_label",
    "author",
    "new_drafts",
    "add_mentioned_drafts",
    "include_related_drafts",
    "clear_cache",
    "clear_config",
    "rebuild_embeddings",
)


def cli_debounce_skip(args: Any) -> Optional[str]:
    """The freshness-debounce skip message for a CLI gather `args` namespace,
    or None to go ahead and gather.

    Bypassed (None) by `--force` and by any flag that makes the run more
    than a plain refresh (`_SOURCE_CHANGE_FLAGS`); `debounce_reason` then
    decides the rest (never-gathered, disabled window, and stale caches all
    return None). Lives here, with the rest of the debounce policy, rather
    than in the CLI module.
    """
    if getattr(args, "force", False):
        return None
    if any(getattr(args, name, None) for name in _SOURCE_CHANGE_FLAGS):
        return None
    return debounce_reason(args.wg)
