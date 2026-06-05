"""Cloudflare D1 backend for the cloud control plane.

`D1Executor` implements the `SqlExecutor` seam (`query` + atomic `batch`)
against Cloudflare D1's HTTP API, so the same `SqlControlPlane` logic that runs
over a local SQLite file runs over D1 — with no new dependency (just `requests`,
already a base dep). D1 *is* SQLite, so the SQL is unchanged. Two endpoints:

  - **`/raw`** for `query`: runs one statement and returns its rows as arrays
    (positional), which the control plane reads by index;
  - **`/query`** with a `{"batch": [...]}` body for `batch`: D1 runs the
    statements in a single transaction (all-or-nothing) — what `publish` needs.

The database is addressed by an `IETF_LLM_CONTROL_DB` locator of the form
`d1://<account_id>/<database_id>`; the API token is a secret read from
`IETF_LLM_CONTROL_DB_TOKEN`. See `docs/storage.md`.

Verification note: the request building and response parsing here are unit-tested
against a mocked HTTP layer; the live D1 transport is not exercised by the test
suite (it needs a Cloudflare account).
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

import requests

from .corpus_control import Row, SqlControlPlane, SqlExecutor, Statement
from .utils import http_session

_API_BASE = "https://api.cloudflare.com/client/v4"
_TIMEOUT = 30.0
_LOCATOR_RE = re.compile(r"^d1://([^/]+)/([^/]+)$")

#: Process-wide guard so the schema DDL is sent at most once per process per D1
#: database, not on every per-request executor construction.
_schema_ensured: set[str] = set()


class D1Error(RuntimeError):
    """A D1 HTTP call failed (transport error, or a `success: false` body)."""


class D1AuthError(D1Error):
    """Cloudflare rejected the D1 API token (HTTP 401/403). Not retryable — the
    fix is a configuration change (a missing, wrong, or under-scoped token), so
    the message names the env var to check rather than surfacing a traceback."""


def _rows(data: Any) -> List[Row]:
    """Extract array-rows from a D1 `/raw` response. `/raw` returns each result's
    rows as arrays; tolerate both the `{columns, rows}` object form and a bare
    list-of-arrays, since either has been seen in the wild."""
    result = data.get("result") or []
    if not result:
        return []
    results = result[0].get("results")
    if isinstance(results, dict):
        raw_rows = results.get("rows") or []
    elif isinstance(results, list):
        raw_rows = results
    else:
        raw_rows = []
    return [tuple(row) for row in raw_rows]


class D1Executor(SqlExecutor):
    """`SqlExecutor` over Cloudflare D1's HTTP API. `session` is injectable for
    tests; by default the shared pooled `requests` session is used."""

    def __init__(
        self,
        account_id: str,
        database_id: str,
        token: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._account = account_id
        self._database = database_id
        self._token = token
        self._session = session

    def _post(self, endpoint: str, body: Any) -> Any:
        session = self._session or http_session()
        url = (
            f"{_API_BASE}/accounts/{self._account}"
            f"/d1/database/{self._database}/{endpoint}"
        )
        resp = session.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise D1AuthError(
                f"Cloudflare rejected the D1 API token (HTTP {resp.status_code} "
                f"{(resp.reason or '').strip()}): it is missing, wrong, or lacks "
                f"D1 edit access for this database. Check IETF_LLM_CONTROL_DB_TOKEN."
            )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", False):
            raise D1Error(f"D1 {endpoint} call failed: {data.get('errors')}")
        return data

    def ensure_schema(self, statements: Sequence[str]) -> None:
        key = f"{self._account}/{self._database}"
        if key in _schema_ensured:
            return
        self.batch([(stmt, ()) for stmt in statements])
        _schema_ensured.add(key)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Row]:
        return _rows(self._post("raw", {"sql": sql, "params": list(params)}))

    def batch(self, statements: Sequence[Statement]) -> None:
        self._post(
            "query",
            {"batch": [{"sql": sql, "params": list(p)} for sql, p in statements]},
        )


class D1ControlPlane(SqlControlPlane):
    """A `SqlControlPlane` over Cloudflare D1, addressed by a
    `d1://<account_id>/<database_id>` locator. The API token defaults to
    `IETF_LLM_CONTROL_DB_TOKEN` (a secret); `session` is injectable for tests."""

    def __init__(
        self,
        locator: str,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        match = _LOCATOR_RE.match(locator)
        if not match:
            raise ValueError(
                "invalid D1 locator (expected d1://<account_id>/<database_id>): "
                f"{locator!r}"
            )
        if not token:
            from . import service_config  # pylint: disable=import-outside-toplevel

            token = service_config.control_db_token()
        if not token:
            raise ValueError(
                "a d1:// control DB needs IETF_LLM_CONTROL_DB_TOKEN (the D1 API token)"
            )
        super().__init__(
            D1Executor(match.group(1), match.group(2), token, session=session)
        )
