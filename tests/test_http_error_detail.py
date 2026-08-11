"""The Cloudflare Ray ID (`cf-ray` response header) is appended to HTTP
error messages. Datatracker sits behind Cloudflare, whose bot protection
sometimes blocks a fetch; IETF support needs the Ray ID to trace a block,
so every surface that reports a fetch failure must carry it when present.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ietf_llm.gather.sources import datatracker
from ietf_llm.live_lookup import cache as live_cache
from ietf_llm.net import transport
from ietf_llm.net.transport import fetch_resource, http_error_detail

# Captured at import (before conftest's autouse `_no_datatracker` stub binds
# over it) so the WARN test below exercises the genuine `_get_json`.
_REAL_GET_JSON = datatracker._get_json  # pylint: disable=protected-access

_URL = "https://datatracker.ietf.org/api/v1/group/group/"


def _response(status: int, headers: Optional[Dict[str, str]] = None) -> requests.Response:
    res = requests.Response()
    res.status_code = status
    res.url = _URL
    res.headers.update(headers or {})
    res._content = b"blocked"  # pylint: disable=protected-access
    return res


def _http_error(status: int, headers: Optional[Dict[str, str]] = None) -> requests.HTTPError:
    try:
        _response(status, headers).raise_for_status()
    except requests.HTTPError as err:
        return err
    raise AssertionError("expected raise_for_status to raise")


def test_ray_id_appended() -> None:
    err = _http_error(403, {"CF-RAY": "8f2b3c4d5e6f7a8b-SYD"})
    detail = http_error_detail(err)
    assert detail.startswith(str(err))
    assert detail.endswith("(Cloudflare Ray ID: 8f2b3c4d5e6f7a8b-SYD)")


def test_header_lookup_is_case_insensitive() -> None:
    err = _http_error(503, {"cf-ray": "0123456789abcdef-MEL"})
    assert "Cloudflare Ray ID: 0123456789abcdef-MEL" in http_error_detail(err)


def test_no_ray_header_means_no_suffix() -> None:
    err = _http_error(500)
    assert http_error_detail(err) == str(err)


def test_error_without_response() -> None:
    err = requests.ConnectionError("connection refused")
    assert http_error_detail(err) == str(err)


def test_non_requests_error() -> None:
    # `_get_json` / `_fetch_json` also format JSON-decode failures this way.
    err = ValueError("Expecting value: line 1 column 1 (char 0)")
    assert http_error_detail(err) == str(err)


def _blocked(url: str, **_kwargs: Any) -> requests.Response:
    return _response(403, {"CF-RAY": "deadbeefcafef00d-SYD"})


def test_fetch_resource_logs_ray_id(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    monkeypatch.setattr(transport, "governed_get", _blocked)
    assert fetch_resource(_URL) is None
    err_out = capsys.readouterr().err
    assert f"Error fetching {_URL}" in err_out
    assert "(Cloudflare Ray ID: deadbeefcafef00d-SYD)" in err_out


def test_get_json_warns_with_ray_id(
    monkeypatch: Any, capsys: Any, isolated_home: Any
) -> None:
    # The Datatracker API path falls back to cached data on failure; before
    # this change the block was silent. isolated_home sandboxes the ETag store.
    assert isolated_home
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    monkeypatch.setattr(datatracker, "_get_json", _REAL_GET_JSON)
    monkeypatch.setattr(datatracker, "_DEFAULT_CACHE", None)
    monkeypatch.setattr(datatracker, "governed_get", _blocked)
    assert datatracker._get_json("/api/v1/group/group/") is None  # pylint: disable=protected-access
    err_out = capsys.readouterr().err
    assert "[WARN]" in err_out
    assert "(Cloudflare Ray ID: deadbeefcafef00d-SYD)" in err_out


def test_live_fetch_json_warns_with_ray_id(monkeypatch: Any, capsys: Any) -> None:
    # Same for the live-lookup fetch seam, whose failures were also silent.
    monkeypatch.delenv("IETF_LLM_LOG_FORMAT", raising=False)
    monkeypatch.setattr(live_cache, "governed_get", _blocked)
    assert live_cache._fetch_json(_URL) is None  # pylint: disable=protected-access
    err_out = capsys.readouterr().err
    assert "[WARN]" in err_out
    assert "(Cloudflare Ray ID: deadbeefcafef00d-SYD)" in err_out
