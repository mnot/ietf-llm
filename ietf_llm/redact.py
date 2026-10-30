"""Removing credentials embedded in URLs from text that leaves the process.

Neutral on purpose (stdlib only): the embedding client, the gather child, the
status recorder and the MCP status renderer all use it, and none of them should
import another's module to do so.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from .log import LogLevel, Verbosity, log

#: A URL's userinfo. Greedy to the last "@" before the path, query or fragment,
#: so a password containing a literal "@" is redacted whole. Linear: the scan
#: stops at the next "/", so overlapping starts cannot rescan the same run.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/?#\s]*@")

#: ``user:password@host/path`` with no scheme, the form ``requests`` quotes in
#: its "No connection adapters" error for a base URL missing ``https://``; also
#: with a protocol-relative ``//`` or a mistyped ``https:/`` in front.
#:
#: Every repeat is bounded. A start is possible after any punctuation, so an
#: unbounded password class is quadratic on a long ``a:!a:!...`` run (seconds
#: at 40 KB); the bounds cap the work per start. They sit above a real
#: username (64) or password/token (256) in a URL.
#:
#: The colon, the "/" after the host and the left-hand boundary keep it off
#: email addresses (no colon), ``git@host:org/repo`` and ``mailto:a@b.example``.
_BARE_USERINFO_RE = re.compile(
    r"(?:(?<![\w/@.:+~%-])|(?<=^//)|(?<=[^\w/]//)|(?<=\w:/))"
    r"(?!mailto:)[\w.~%+-]{1,64}:[^\s/'\"<>]{0,256}@"
    r"(?=[\w.\[-][^\s/@]{0,255}/)"
)

#: What replaces a status record's free text when it cannot be sanitised.
PLACEHOLDER = "[redacted: could not sanitise]"

#: The status record's free-text fields (see `redact_record`).
_FREE_TEXT_FIELDS = ("error", "notes", "stage_detail", "detail")


def redact_userinfo(text: str) -> str:
    """``text`` with any ``user:password@`` in a URL replaced by ``***@``.

    An operator can put credentials in a configured base URL; ``requests``
    echoes the URL in its exception messages, and those reach logs, the fleet
    gather status and tool output.

    Not covered, because the text itself no longer shows where the secret
    starts or ends: a "/", "?" or "#" in the password (``https://u:pa/ss@host``,
    ``https://u:pa?ss@host``), which ``requests`` reports as a bare
    ``'u:pa' is not a valid host or port``.
    """
    return _BARE_USERINFO_RE.sub("***@", _URL_USERINFO_RE.sub("***@", text))


def redact_value(value: Any) -> Any:
    """``value`` with `redact_userinfo` applied to every string in it, descending
    into dicts and lists. Returns a copy; the input is untouched."""
    if isinstance(value, str):
        return redact_userinfo(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """A status record with credentials removed from every string in it. Never
    raises: if redaction itself fails, the free-text fields are replaced by
    `PLACEHOLDER` and a warning is logged, so unsanitised text is never written."""
    try:
        return redact_value(record)  # type: ignore[no-any-return]
    except Exception as err:  # pylint: disable=broad-except
        log(
            f"gather: could not sanitise a status record ({type(err).__name__}); "
            "its free-text fields were replaced",
            Verbosity.STATUS,
            level=LogLevel.WARN,
        )
        safe = dict(record)
        for field in _FREE_TEXT_FIELDS:
            if safe.get(field) is None:
                continue
            safe[field] = [PLACEHOLDER] if field == "notes" else PLACEHOLDER
        return safe
