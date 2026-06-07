"""Serve-side RED metrics and the GET /metrics scrape route (issue #40).

Covers the in-memory registry (per-tool RED + embed backend), the
Prometheus text exposition (histogram cumulativity, error counters,
label escaping), the two instrumented chokepoints (`_offload` and the
remote `_embed_batch`) actually recording, and that the route emits the
metric families plus a per-corpus freshness gauge derived from the
last-gathered sentinels.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ietf_llm import mcp_server, oai_compat, serve_metrics
from ietf_llm.embeddings.models import _OpenAICompatEmbeddingModel
from ietf_llm.utils import get_cache_dir


@pytest.fixture(autouse=True)
def _reset_metrics():
    # The registry is process-global; isolate every test from the others.
    serve_metrics.reset()
    yield
    serve_metrics.reset()


class _FakeServer:
    def streamable_http_app(self) -> Any:
        return Starlette(routes=[])


def _seed_corpus(wg: str, sentinel: str | None) -> None:
    base = os.path.join(get_cache_dir(), wg)
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    if sentinel is not None:
        path = os.path.join(base, "last-gathered")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sentinel)


def _metric_value(body: str, line_prefix: str) -> float:
    for line in body.splitlines():
        if line.startswith(line_prefix) and not line.startswith("#"):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"no metric line starting {line_prefix!r}")


# --- registry + exposition --------------------------------------------------


def test_render_empty_has_families_but_no_series():
    body = serve_metrics.render()
    assert "# TYPE ietf_llm_tool_requests_total counter" in body
    assert "# TYPE ietf_llm_embed_latency_seconds histogram" in body
    # No tool recorded => no per-tool series line.
    assert "ietf_llm_tool_requests_total{" not in body
    # Embed counters always present (single series, no label).
    assert "ietf_llm_embed_requests_total 0" in body


def test_record_tool_counts_requests_and_errors():
    serve_metrics.record_tool("overview", 0.2, error=False)
    serve_metrics.record_tool("overview", 0.3, error=True)
    serve_metrics.record_tool("search_corpus", 1.0, error=False)
    body = serve_metrics.render()
    assert _metric_value(body, 'ietf_llm_tool_requests_total{tool="overview"}') == 2
    assert _metric_value(body, 'ietf_llm_tool_errors_total{tool="overview"}') == 1
    assert _metric_value(
        body, 'ietf_llm_tool_errors_total{tool="search_corpus"}'
    ) == 0
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_count{tool="overview"}'
    ) == 2
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_sum{tool="overview"}'
    ) == pytest.approx(0.5)


def test_histogram_buckets_are_cumulative():
    # 0.2 lands in le="0.25"+; 3.0 lands in le="5"+; both in +Inf.
    serve_metrics.record_tool("t", 0.2, error=False)
    serve_metrics.record_tool("t", 3.0, error=False)
    body = serve_metrics.render()
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="t",le="0.1"}'
    ) == 0
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="t",le="0.25"}'
    ) == 1
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="t",le="5"}'
    ) == 2
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="t",le="+Inf"}'
    ) == 2


def test_histogram_buckets_reach_the_tool_deadline():
    # A near-deadline call (default IETF_LLM_TOOL_TIMEOUT is 120s) must land
    # in a real bucket, not collapse into +Inf — so the 60/120 bounds exist
    # and a 90s observation sits above le="60" but at/below le="120".
    serve_metrics.record_tool("slow", 90.0, error=False)
    body = serve_metrics.render()
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="slow",le="60"}'
    ) == 0
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="slow",le="120"}'
    ) == 1
    assert _metric_value(
        body, 'ietf_llm_tool_latency_seconds_bucket{tool="slow",le="+Inf"}'
    ) == 1


def test_embed_metrics_recorded():
    serve_metrics.record_embed(0.05, error=False)
    serve_metrics.record_embed(0.5, error=True)
    body = serve_metrics.render()
    assert _metric_value(body, "ietf_llm_embed_requests_total") == 2
    assert _metric_value(body, "ietf_llm_embed_errors_total") == 1
    assert _metric_value(body, "ietf_llm_embed_latency_seconds_count") == 2


def test_label_value_escaping():
    serve_metrics.record_tool('weird"name', 0.1, error=False)
    body = serve_metrics.render()
    assert 'tool="weird\\"name"' in body


def test_freshness_gauge_emitted_for_passed_ages():
    body = serve_metrics.render([("tls", 3600), ("httpbis", 10)])
    assert _metric_value(
        body, 'ietf_llm_corpus_last_gathered_age_seconds{corpus="tls"}'
    ) == 3600
    assert _metric_value(
        body, 'ietf_llm_corpus_last_gathered_age_seconds{corpus="httpbis"}'
    ) == 10


# --- chokepoint wiring ------------------------------------------------------


def test_offload_records_tool_metric():
    def my_tool() -> str:
        return "ok"

    result = asyncio.run(mcp_server._offload(my_tool))
    assert result == "ok"
    body = serve_metrics.render()
    assert _metric_value(
        body, 'ietf_llm_tool_requests_total{tool="my_tool"}'
    ) == 1
    assert _metric_value(body, 'ietf_llm_tool_errors_total{tool="my_tool"}') == 0


def test_offload_records_error_on_raise():
    def boom() -> str:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        asyncio.run(mcp_server._offload(boom))
    body = serve_metrics.render()
    assert _metric_value(body, 'ietf_llm_tool_errors_total{tool="boom"}') == 1


def _model() -> _OpenAICompatEmbeddingModel:
    return _OpenAICompatEmbeddingModel(
        "m", "https://host/v1", {}, batch_size=96, timeout=5.0, max_retries=0
    )


def test_embed_batch_records_success(monkeypatch):
    monkeypatch.setattr(
        oai_compat,
        "post_json_with_retry",
        lambda *a, **k: {"data": [{"index": 0, "embedding": [1.0]}]},
    )
    _model().embed("hi")
    body = serve_metrics.render()
    assert _metric_value(body, "ietf_llm_embed_requests_total") == 1
    assert _metric_value(body, "ietf_llm_embed_errors_total") == 0


def test_embed_batch_records_error(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(oai_compat, "post_json_with_retry", boom)
    with pytest.raises(RuntimeError):
        _model().embed("hi")
    body = serve_metrics.render()
    assert _metric_value(body, "ietf_llm_embed_requests_total") == 1
    assert _metric_value(body, "ietf_llm_embed_errors_total") == 1


# --- the /metrics route -----------------------------------------------------


def test_metrics_route_content_type_and_families(isolated_home):
    client = TestClient(mcp_server._http_app(_FakeServer()))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]
    assert "ietf_llm_tool_latency_seconds" in resp.text
    assert "ietf_llm_embed_requests_total" in resp.text


def test_metrics_route_freshness_gauge(isolated_home):
    _seed_corpus("tls", "2025-01-01T00:00:00Z")
    _seed_corpus("quic", None)  # counts as a corpus but no sentinel
    client = TestClient(mcp_server._http_app(_FakeServer()))
    body = client.get("/metrics").text
    assert (
        'ietf_llm_corpus_last_gathered_age_seconds{corpus="tls"}' in body
    )
    # Untracked corpus is omitted from the gauge.
    assert "corpus=\"quic\"" not in body
