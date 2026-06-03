"""Tests for the shared OpenAI-compatible HTTP helpers.

parse_retry_after (both Retry-After forms), build_headers (bearer token
plus JSON header map, with bad-JSON tolerated), and the env_int /
env_float coercers.
"""

from __future__ import annotations

from ietf_llm import oai_compat
from ietf_llm.utils import Verbosity


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


def test_env_int_and_float(monkeypatch):
    monkeypatch.delenv("IETF_LLM_X", raising=False)
    assert oai_compat.env_int("IETF_LLM_X", 7) == 7
    assert oai_compat.env_float("IETF_LLM_X", 1.5) == 1.5
    monkeypatch.setenv("IETF_LLM_X", "42")
    assert oai_compat.env_int("IETF_LLM_X", 7) == 42
    monkeypatch.setenv("IETF_LLM_X", "bad")
    assert oai_compat.env_int("IETF_LLM_X", 7) == 7
