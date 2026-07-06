"""Tests for the shared OpenAI-compatible HTTP helpers.

parse_retry_after (both Retry-After forms), build_headers (bearer token
plus JSON header map, with bad-JSON tolerated), and the env_int /
env_float coercers.
"""

from __future__ import annotations

import pytest

from ietf_llm.embeddings import oai_compat
from ietf_llm.utils import Verbosity


class _Resp:
    def __init__(self, status, reason="", data=None):
        self.status_code = status
        self.reason = reason
        self._data = data if data is not None else {"ok": True}
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def test_parse_retry_after_delta_seconds():
    assert oai_compat.parse_retry_after("5") == 5.0
    assert oai_compat.parse_retry_after("  12  ") == 12.0
    assert oai_compat.parse_retry_after("-3") == 0.0  # never negative


def test_parse_retry_after_http_date():
    assert oai_compat.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0.0
    assert oai_compat.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_parse_retry_after_unparseable():
    assert oai_compat.parse_retry_after("not-a-date") == 0.0
    assert oai_compat.parse_retry_after("") == 0.0


def _capture_backoff(monkeypatch, jitter=0.4):
    slept = []
    monkeypatch.setattr(oai_compat.random, "uniform", lambda _a, _b: jitter)
    monkeypatch.setattr(oai_compat.time, "sleep", slept.append)
    return slept


def test_backoff_jitters_on_top_of_retry_after(monkeypatch):
    # An account-level limit throttles concurrent callers together and hands
    # them the same Retry-After; the jitter must spread their wake-ups, so the
    # actual sleep is Retry-After + jitter, not the bare header value.
    slept = _capture_backoff(monkeypatch, jitter=0.4)
    resp = _Resp(429)
    resp.headers = {"Retry-After": "5"}
    oai_compat._sleep_backoff(0, resp)
    assert slept == [5.4]


def test_backoff_jitters_on_exponential_path(monkeypatch):
    # No Retry-After: exponential base (min(30, 2**attempt)) plus the jitter.
    slept = _capture_backoff(monkeypatch, jitter=0.4)
    oai_compat._sleep_backoff(2, None)
    assert slept == [4.4]


def test_build_headers_bearer_and_map():
    h = oai_compat.build_headers("tok", '{"cf-aig-authorization": "g"}',
                                 "IETF_LLM_X_HEADERS", Verbosity.QUIET)
    assert h["Authorization"] == "Bearer tok"
    assert h["cf-aig-authorization"] == "g"


def test_build_headers_empty():
    assert oai_compat.build_headers("", "", "IETF_LLM_X_HEADERS", Verbosity.QUIET) == {}


def test_build_headers_bad_json_ignored():
    h = oai_compat.build_headers("tok", "not-json", "IETF_LLM_X_HEADERS", Verbosity.QUIET)
    assert h == {"Authorization": "Bearer tok"}


def test_build_headers_non_object_json_ignored():
    h = oai_compat.build_headers("", "[1, 2]", "IETF_LLM_X_HEADERS", Verbosity.QUIET)
    assert h == {}


@pytest.mark.parametrize("status", [401, 403, 407])
def test_auth_status_raises_actionable_error_without_retrying(monkeypatch, status):
    calls = {"n": 0}

    def fake(url, headers, json, timeout):
        calls["n"] += 1
        return _Resp(status, reason="Unauthorized")

    monkeypatch.setattr(oai_compat.requests, "post", fake)
    with pytest.raises(oai_compat.UpstreamAuthError) as exc:
        oai_compat.post_json_with_retry(
            "https://host/v1/embeddings", {}, {},
            timeout=5.0, max_retries=5, auth_hint="IETF_LLM_EMBED_TOKEN",
        )
    # Not retried (a bad token will not fix itself), and names the env var.
    assert calls["n"] == 1
    assert "IETF_LLM_EMBED_TOKEN" in str(exc.value)
    assert str(status) in str(exc.value)


def test_non_auth_4xx_still_raises_for_status(monkeypatch):
    # A 400 is a request bug, not an auth failure: it must not be wrapped as
    # UpstreamAuthError, and (since it is not 429/5xx) is not retried.
    monkeypatch.setattr(
        oai_compat.requests, "post",
        lambda url, headers, json, timeout: _BadReq(),
    )
    with pytest.raises(Exception) as exc:
        oai_compat.post_json_with_retry(
            "https://host/v1/embeddings", {}, {}, timeout=5.0, max_retries=2,
        )
    assert not isinstance(exc.value, oai_compat.UpstreamAuthError)


class _BadReq(_Resp):
    def __init__(self):
        super().__init__(400, reason="Bad Request")

    def raise_for_status(self):
        import requests

        raise requests.HTTPError("400")


def test_env_int_and_float(monkeypatch):
    monkeypatch.delenv("IETF_LLM_X", raising=False)
    assert oai_compat.env_int("IETF_LLM_X", 7) == 7
    assert oai_compat.env_float("IETF_LLM_X", 1.5) == 1.5
    monkeypatch.setenv("IETF_LLM_X", "42")
    assert oai_compat.env_int("IETF_LLM_X", 7) == 42
    monkeypatch.setenv("IETF_LLM_X", "bad")
    assert oai_compat.env_int("IETF_LLM_X", 7) == 7
