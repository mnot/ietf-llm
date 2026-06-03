"""Shared plumbing for OpenAI-compatible HTTP backends.

Both the remote embedding backend (``POST {base}/embeddings``) and the
remote summariser (``POST {base}/chat/completions``) speak the same
OpenAI HTTP contract: Bearer auth, an optional JSON header map for an
API gateway, and 429 / 5xx retry that honours ``Retry-After``. This
module holds the parts that are identical between them so the two
backends stay in lockstep, and so secrets only ever come from the
environment (never code or persisted config).
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, cast

import requests

from .utils import LogLevel, Verbosity, log


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def parse_retry_after(value: str) -> float:
    """Seconds to wait from a ``Retry-After`` header (RFC 9110 10.2.3).

    Handles both permitted forms: delta-seconds and an HTTP-date
    (IMF-fixdate). Returns 0.0 for anything unparseable, so the caller
    falls back to its own exponential backoff.
    """
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def build_headers(
    token: str, raw_headers: str, headers_var: str, verbose: Verbosity
) -> dict[str, str]:
    """Assemble request headers from a bearer token and a JSON header map.

    A gateway can require a header alongside the provider bearer token,
    so auth is a header map (``headers_var`` is the environment variable
    the JSON came from, used only for error messages) rather than a
    single Authorization line -- which avoids a rebuild to add one.
    """
    headers: dict[str, str] = {}
    token = token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    raw_headers = raw_headers.strip()
    if raw_headers:
        try:
            extra = json.loads(raw_headers)
        except json.JSONDecodeError:
            log(
                f"{headers_var} is not valid JSON; ignoring it.",
                verbose,
                level=LogLevel.ERROR,
            )
        else:
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
            else:
                log(
                    f"{headers_var} must be a JSON object; ignoring it.",
                    verbose,
                    level=LogLevel.ERROR,
                )
    return headers


def _sleep_backoff(attempt: int, resp: requests.Response | None) -> None:
    # Honour Retry-After when the server sends it, else exponential
    # backoff with jitter. The rate limit is account-level, so several
    # concurrent callers share the budget -- jitter de-synchronises their
    # retries instead of having them all wake together.
    delay = 0.0
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            delay = parse_retry_after(retry_after)
    if delay <= 0.0:
        delay = min(30.0, 2.0**attempt) + random.uniform(0.0, 1.0)
    time.sleep(delay)


def post_json_with_retry(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """POST ``payload`` as JSON, retrying 429 / 5xx and connection errors.

    Retries up to ``max_retries`` times, honouring ``Retry-After`` and
    otherwise backing off exponentially with jitter. Returns the parsed
    JSON body; raises the last error once retries are exhausted.
    """
    max_retries = max(0, max_retries)
    attempt = 0
    while True:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException:
            if attempt >= max_retries:
                raise
            _sleep_backoff(attempt, None)
            attempt += 1
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt >= max_retries:
                resp.raise_for_status()
            _sleep_backoff(attempt, resp)
            attempt += 1
            continue
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())
