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

#: The scalar fields of a status record (`lifecycle.new_status`, plus what the
#: runner sets) that carry no free text: the fallback in `redact_record` keeps
#: only these.
_SAFE_STATUS_FIELDS = frozenset(
    {
        "corpus",
        "state",
        "driver",
        "pid",
        "started",
        "updated",
        "finished",
        "stage",
        "stage_index",
        "stage_total",
        "cancel_requested",
        "cancel_token_sha256",
    }
)

#: Fields that carry free text or caller input; the fallback keeps the key and
#: replaces the value.
_PLACEHOLDER_STATUS_FIELDS = ("error", "stage_detail", "notes", "spec")

_SCALARS = (str, int, float, bool, type(None))


def redact_userinfo(text: str) -> str:
    """``text`` with any ``user:password@`` in a URL replaced by ``***@``.

    An operator can put credentials in a configured base URL; ``requests``
    echoes the URL in its exception messages, and those reach logs, the fleet
    gather status and tool output.

    Not covered:

    - a "/" in the password (``https://u:pa/ss@host``): ``requests`` reports
      only the bare ``'u:pa' is not a valid host or port``, and nothing in that
      text says where the secret ends. ("?" and "#" are handled when a scheme
      is present.)
    - a password over 256 characters in the scheme-less ``user:pw@host/path``
      form (a JWT, say); the bound keeps the pattern linear-time. The
      ``scheme://user:pw@host`` form has no length limit.
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
    raises: if redaction itself fails, a warning is logged and the record is
    reduced to an allowlist of its scalar fields, with `error`, `stage_detail`,
    `notes` and `spec` replaced by `PLACEHOLDER` and everything else dropped, so
    nothing unsanitised is ever written."""
    try:
        return redact_value(record)
    except Exception as err:  # noqa: BLE001
        log(
            f"gather: could not sanitise a status record ({type(err).__name__}); "
            "its free-text fields were replaced",
            Verbosity.STATUS,
            level=LogLevel.WARN,
        )
        safe: Dict[str, Any] = {}
        for key in _SAFE_STATUS_FIELDS:
            value = record.get(key)
            if isinstance(value, _SCALARS):
                safe[key] = value
        for key in _PLACEHOLDER_STATUS_FIELDS:
            if record.get(key) is not None:
                safe[key] = [PLACEHOLDER] if key == "notes" else PLACEHOLDER
        return safe
