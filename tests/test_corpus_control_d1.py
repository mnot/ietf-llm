"""Tests for the Cloudflare D1 control-plane adapter: request building and
response parsing against a mocked HTTP session. (Live D1 transport is not
exercised here — it needs a Cloudflare account.)"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import requests

import ietf_llm.corpus_control_d1 as d1mod
from ietf_llm.corpus_control_d1 import (
    D1AuthError,
    D1ControlPlane,
    D1Error,
    D1Executor,
)


class _Resp:
    def __init__(
        self, payload: Any, ok: bool = True, status_code: int = 200, reason: str = ""
    ) -> None:
        self._payload = payload
        self._ok = ok
        self.status_code = status_code
        self.reason = reason

    def raise_for_status(self) -> None:
        if not self._ok:
            raise requests.HTTPError("http error")

    def json(self) -> Any:
        return self._payload


class _Session:
    """Records posts and returns a canned payload for each."""

    def __init__(
        self, payload: Any, ok: bool = True, status_code: int = 200, reason: str = ""
    ) -> None:
        self._payload = payload
        self._ok = ok
        self._status = status_code
        self._reason = reason
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, json: Any, headers: Any, timeout: Any) -> _Resp:
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _Resp(self._payload, self._ok, self._status, self._reason)


def test_query_builds_raw_request_and_parses_rows() -> None:
    sess = _Session(
        {"success": True, "result": [{"results": {"rows": [["node-a", 1000.0]]}}]}
    )
    ex = D1Executor("acc", "db", "tok", session=sess)  # type: ignore[arg-type]
    rows = ex.query("SELECT owner, expires_at FROM gather_lease WHERE corpus=?", ("tls",))
    assert rows == [("node-a", 1000.0)]
    call = sess.calls[0]
    assert call["url"].endswith("/accounts/acc/d1/database/db/raw")
    assert call["json"] == {
        "sql": "SELECT owner, expires_at FROM gather_lease WHERE corpus=?",
        "params": ["tls"],
    }
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_rows_tolerate_bare_list_form() -> None:
    sess = _Session({"success": True, "result": [{"results": [["a", "b"]]}]})
    ex = D1Executor("acc", "db", "tok", session=sess)  # type: ignore[arg-type]
    assert ex.query("SELECT a, b FROM t") == [("a", "b")]


def test_batch_builds_query_batch_request() -> None:
    sess = _Session({"success": True, "result": []})
    ex = D1Executor("acc", "db", "tok", session=sess)  # type: ignore[arg-type]
    ex.batch([("INSERT INTO a VALUES (?)", ("x",)), ("UPDATE b SET y=?", ("z",))])
    call = sess.calls[0]
    assert call["url"].endswith("/d1/database/db/query")
    assert call["json"] == {
        "batch": [
            {"sql": "INSERT INTO a VALUES (?)", "params": ["x"]},
            {"sql": "UPDATE b SET y=?", "params": ["z"]},
        ]
    }


def test_unsuccessful_body_raises() -> None:
    sess = _Session({"success": False, "errors": [{"message": "nope"}]})
    ex = D1Executor("acc", "db", "tok", session=sess)  # type: ignore[arg-type]
    with pytest.raises(D1Error):
        ex.query("SELECT 1")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_token_raises_actionable_auth_error(status: int) -> None:
    sess = _Session({}, status_code=status, reason="Forbidden")
    ex = D1Executor("acc", "db", "tok", session=sess)  # type: ignore[arg-type]
    with pytest.raises(D1AuthError) as exc:
        ex.query("SELECT 1")
    assert "IETF_LLM_CONTROL_DB_TOKEN" in str(exc.value)
    assert str(status) in str(exc.value)


#: A syntactically valid D1 Database ID (UUID) for locators under test.
_DB_UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def test_invalid_locator_raises() -> None:
    with pytest.raises(ValueError):
        D1ControlPlane("d1://only-one-part", token="tok")


def test_database_name_instead_of_uuid_is_rejected() -> None:
    # The reported mix-up: the database *name* where the Database ID (UUID)
    # belongs. Caught up front with a message that names the fix.
    with pytest.raises(ValueError) as exc:
        D1ControlPlane("d1://acc/my-database", token="tok")
    msg = str(exc.value)
    assert "UUID" in msg and "Database ID" in msg


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IETF_LLM_CONTROL_DB_TOKEN", raising=False)
    with pytest.raises(ValueError):
        D1ControlPlane(f"d1://acc/{_DB_UUID}")


def test_construct_ensures_schema_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d1mod, "_schema_ensured", set())
    sess = _Session({"success": True, "result": []})
    D1ControlPlane(f"d1://acc/{_DB_UUID}", token="tok", session=sess)  # type: ignore[arg-type]
    # Construction ran ensure_schema as a single batch (the CREATE TABLEs).
    assert sess.calls
    assert sess.calls[0]["url"].endswith(f"/d1/database/{_DB_UUID}/query")
    assert "batch" in sess.calls[0]["json"]
