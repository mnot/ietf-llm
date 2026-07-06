"""The gather ``--months`` window policy.

Validate a requested window (`months_request_error` / `months_request_caution`)
and resolve the effective one after config merge (`resolve_months`). `months=0`
means all-history and is honoured only with ``--force``. Shared by the
`ietf-llm` CLI, the MCP `start_gather` tool, and the readers that surface the
default window. Stdlib-only.
"""

from __future__ import annotations

from typing import Optional, Tuple

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
