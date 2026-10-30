"""The gather transport's Retry-After bound and the `governed_get` chokepoint.

A real (loopback) HTTP server so the retrying urllib3 adapter is exercised
end to end; `time.sleep` inside urllib3 is stubbed so nothing actually waits.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import requests

from ietf_llm import net
from ietf_llm.gather.sources import repo_discovery as rd
from ietf_llm.net import http_metrics, transport

Script = List[Tuple[int, Dict[str, str], bytes]]


class _Server:
    def __init__(self, script: Script) -> None:
        self.script = list(script)
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                step = outer.script[min(outer.hits, len(outer.script) - 1)]
                status, headers, body = step
                outer.hits += 1
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.url = f"http://{self.host}/x"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: List[_Server] = []

    def make(script: Script) -> _Server:
        srv = _Server(script)
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.close()


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    calls: List[float] = []
    monkeypatch.setattr("urllib3.util.retry.time.sleep", calls.append)
    return calls


def test_oversized_retry_after_is_not_slept_on(serve: Any, slept: List[float]) -> None:
    srv = serve([(429, {"Retry-After": "3600"}, b"slow down")])
    resp = net.governed_get(srv.url, timeout=5)
    # The 429 comes straight back: one request, no sleep, no retry.
    assert (resp.status_code, srv.hits, slept) == (429, 1, [])


def test_oversized_retry_after_http_date(serve: Any, slept: List[float]) -> None:
    srv = serve([(503, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}, b"")])
    resp = net.governed_get(srv.url, timeout=5)
    assert (resp.status_code, srv.hits, slept) == (503, 1, [])


def test_retry_after_within_bound_is_honoured(serve: Any, slept: List[float]) -> None:
    srv = serve([(429, {"Retry-After": "2"}, b""), (200, {}, b"ok")])
    resp = net.governed_get(srv.url, timeout=5)
    assert (resp.status_code, srv.hits, slept) == (200, 2, [2])


def test_retry_after_at_the_limit_is_honoured(serve: Any, slept: List[float]) -> None:
    limit = transport.RETRY_AFTER_MAX
    srv = serve([(429, {"Retry-After": str(limit)}, b""), (200, {}, b"")])
    assert net.governed_get(srv.url, timeout=5).status_code == 200
    assert slept == [limit]


def test_unparseable_retry_after_is_left_to_urllib3(serve: Any, slept: List[float]) -> None:
    srv = serve([(500, {"Retry-After": "soon"}, b"")])
    with pytest.raises(requests.RequestException):
        net.governed_get(srv.url, timeout=5)
    assert srv.hits == 1


# --- governed_get is the metered chokepoint --------------------------------


def test_governed_get_records_response(serve: Any) -> None:
    srv = serve([(200, {}, b"hello")])
    m = http_metrics.reset()
    net.governed_get(srv.url, timeout=5)
    assert (m.ok, m.errors, m.revalidated, m.bytes_received) == (1, 0, 0, 5)
    assert m.host_requests[srv.host] == 1


def test_governed_get_records_error_status_and_bytes(serve: Any) -> None:
    srv = serve([(404, {}, b"nope")])
    m = http_metrics.reset()
    net.governed_get(srv.url, timeout=5)
    assert (m.ok, m.errors, m.bytes_received) == (0, 1, 4)


def test_governed_get_records_304_as_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _R:
        status_code = 304
        content = b""
        headers: Dict[str, str] = {}

    monkeypatch.setattr(net.http_session(), "get", lambda *a, **k: _R())
    m = http_metrics.reset()
    net.governed_get("https://datatracker.ietf.org/x")
    assert (m.ok, m.revalidated, m.errors) == (0, 1, 0)


def test_governed_get_records_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(net.http_session(), "get", boom)
    m = http_metrics.reset()
    with pytest.raises(requests.ConnectionError):
        net.governed_get("https://github.com/x")
    assert (m.ok, m.errors, m.bytes_received) == (0, 1, 0)
    assert m.host_requests["github.com"] == 1


def test_governed_get_meters_a_stream_by_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _R:
        status_code = 200
        headers = {"Content-Length": "1234"}

        @property
        def content(self) -> bytes:  # reading it would consume the stream
            raise AssertionError("streamed body must not be read")

    monkeypatch.setattr(net.http_session(), "get", lambda *a, **k: _R())
    m = http_metrics.reset()
    net.governed_get("https://www.ietf.org/slides.pdf", stream=True)
    assert (m.ok, m.bytes_received) == (1, 1234)


def test_fetch_resource_does_not_double_count(serve: Any) -> None:
    srv = serve([(200, {}, b"abc")])
    m = http_metrics.reset()
    assert net.fetch_resource(srv.url) is not None
    assert m.total == 1


def test_github_discovery_egress_reaches_gather_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _R:
        status_code = 200
        content = b'{"a": 1}'
        headers: Dict[str, str] = {}

        @staticmethod
        def json() -> Any:
            return {"a": 1}

    seen: Dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> _R:
        seen.update(kwargs)
        return _R()

    monkeypatch.setattr(net.http_session(), "get", fake_get)
    http_metrics.reset()
    status, data = rd._GhClient(token=None).get(
        "https://api.github.com/orgs/x/repos", params={"per_page": 100}
    )
    assert (status, data) == (200, {"a": 1})
    assert seen["params"] == {"per_page": 100}
    http_metrics.persist(str(tmp_path))
    doc = json.loads((tmp_path / "gather-metrics.json").read_text())
    assert doc["by_host"]["api.github.com"] == {"requests": 1, "bytes": 8}


def test_pattern_table_is_bounded() -> None:
    # Digits fold to `#`, so make the path words themselves distinct.
    m = http_metrics.HttpMetrics()
    extra = 50
    total = http_metrics._MAX_PATTERNS + extra
    for i in range(total):
        word = "".join(chr(97 + i // 26**k % 26) for k in range(3))
        m.record(f"https://datatracker.ietf.org/doc/{word}/", 200, 1)
    assert len(m.patterns) == http_metrics._MAX_PATTERNS + 1
    assert m.patterns[http_metrics._OTHER] == extra
    assert m.total == total


def test_transport_imports_without_urllib3_2_only_names() -> None:
    # `BaseHTTPResponse` exists only in urllib3 2.x; requests still permits
    # 1.26, so the module must not need it at runtime.
    code = (
        "import urllib3.response as r; del r.BaseHTTPResponse; "
        "import ietf_llm.net.transport"
    )
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
