"""Digest-specific helpers (issue state, size formatting), plus
re-exports of the generic text helpers so existing callers continue
to import from `ietf_llm.digest`.
"""

from __future__ import annotations

from typing import Any

# Re-exported for backward compatibility (tests and external callers
# import these from ietf_llm.digest); the real definitions live in
# ietf_llm.text so non-digest modules can use them without triggering
# the digest package's other imports.
from ..text import _normalize_subject, _parse_date, _short_addr

__all__ = [
    "_normalize_subject",
    "_parse_date",
    "_short_addr",
    "_state_is_open",
    "_fmt_size",
]


def _state_is_open(state: Any) -> bool:
    """True iff an issue state value represents 'open'.

    GitHub's archive JSON uses both lowercase ('open'/'closed', the
    REST API convention) and uppercase ('OPEN'/'CLOSED', the GraphQL
    convention) depending on which exporter produced the archive.
    Normalise here so downstream sorting and counting work either way.
    """
    return isinstance(state, str) and state.strip().lower() == "open"


def _fmt_size(num: int) -> str:
    """Render a byte count as a short human-readable string (e.g. '1.2MB')."""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
