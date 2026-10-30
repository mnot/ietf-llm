"""Removing credentials embedded in URLs from text that leaves the process.

Neutral on purpose (stdlib only): the embedding client, the gather child and the
status recorder all use it, and none of them should import another's module to
do so.
"""

from __future__ import annotations

import re
from typing import Any

#: A URL's userinfo. Greedy to the last "@" before the path, query or fragment,
#: so a password containing a literal "@" is redacted whole.
_URL_USERINFO_RE = re.compile(r"(?<=://)[^/?#\s]*@")

#: ``user:password@host/path`` with no scheme, the form ``requests`` quotes in
#: its "No connection adapters" error for a base URL missing ``https://``. The
#: colon, the "/" after the host, and the left-hand boundary are what keep it
#: off email addresses (no colon), ``git@host:org/repo`` (the lookbehind) and
#: ``mailto:a@b.example``.
_BARE_USERINFO_RE = re.compile(
    r"(?<![\w/@.:+~%-])(?!mailto:)[\w.~%+-]+:[^\s/'\"<>]*@(?=[\w.\[-][^\s/@]*/)"
)


def redact_userinfo(text: str) -> str:
    """``text`` with any ``user:password@`` in a URL replaced by ``***@``.

    An operator can put credentials in a configured base URL; ``requests``
    echoes the URL in its exception messages, and those reach logs, the fleet
    gather status and tool output.

    Not covered, because nothing in the text marks where the secret ends: an
    unencoded "/" in the password (``https://u:pa/ss@host``), which ``requests``
    reports as a bare ``'u:pa' is not a valid host or port``.
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
