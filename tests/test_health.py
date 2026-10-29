"""R18: health/readiness endpoint. A GET /health route reports readiness
(index dir mounted + usable) with no upstream embed call, so a fronting
load balancer / orchestrator can probe the container.
"""

from __future__ import annotations
from ietf_llm import mcp

import asyncio
import os
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from ietf_llm import __version__, serve_metrics
from ietf_llm.paths import get_cache_dir


def _seed_corpus(home: Any, wg: str, sentinel: str | None) -> None:
    """Materialise a corpus under the sandbox cache: a `files/` dir (what
    `_list_wgs` keys on) and, when given, a `last-gathered` sentinel."""
    base = os.path.join(get_cache_dir(), wg)
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    if sentinel is not None:
        with open(os.path.join(base, "last-gathered"), "w", encoding="utf-8") as fh:
            fh.write(sentinel)


class _FakeServer:
    """streamable_http_app() returns a plain Starlette app, so /health can
    be exercised without the real MCP session-manager lifespan."""

    def streamable_http_app(self, **_kwargs: Any) -> Any:
        return Starlette(routes=[])


def test_readiness_ready(isolated_home):
    ready, detail = mcp.serve._readiness()
    assert ready is True
    assert detail["index_dir_usable"] is True
    assert "embed_endpoint_configured" in detail


def test_readiness_not_ready_when_index_dir_missing(monkeypatch):
    monkeypatch.setattr(mcp.serve, "get_index_dir", lambda: "/no/such/dir/xyzzy")
    ready, detail = mcp.serve._readiness()
    assert ready is False
    assert detail["index_dir_usable"] is False


def test_readiness_reports_remote_endpoint(monkeypatch, isolated_home):
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    _, detail = mcp.serve._readiness()
    assert detail["embed_endpoint_configured"] is True


def test_health_route_ok(isolated_home):
    client = TestClient(mcp.serve._http_app(_FakeServer()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_route_unavailable(monkeypatch):
    monkeypatch.setattr(mcp.serve, "get_index_dir", lambda: "/no/such/dir/xyzzy")
    client = TestClient(mcp.serve._http_app(_FakeServer()))
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"


def test_readiness_reports_version(isolated_home):
    _, detail = mcp.serve._readiness()
    assert detail["version"] == __version__


def test_corpora_freshness_empty(isolated_home):
    summary = mcp.serve._corpora_freshness()
    assert summary == {"count": 0, "tracked": 0, "oldest": None, "newest": None}


def test_corpora_freshness_summary(isolated_home):
    # Two tracked corpora at different ages, plus one with no sentinel:
    # it counts but is not tracked, and never becomes oldest/newest.
    _seed_corpus(isolated_home, "tls", "2025-01-01T00:00:00Z")
    _seed_corpus(isolated_home, "httpbis", "2026-01-01T00:00:00Z")
    _seed_corpus(isolated_home, "quic", None)

    summary = mcp.serve._corpora_freshness()
    assert summary["count"] == 3
    assert summary["tracked"] == 2
    assert summary["oldest"]["corpus"] == "tls"
    assert summary["newest"]["corpus"] == "httpbis"
    # Earlier gather => larger age; both are real, positive second counts.
    assert summary["oldest"]["age_seconds"] > summary["newest"]["age_seconds"] > 0
    assert summary["oldest"]["last_gathered"] == "2025-01-01T00:00:00Z"


def test_readiness_reports_gathers_inflight(isolated_home):
    serve_metrics.reset()
    _, detail = mcp.serve._readiness()
    assert detail["gathers_inflight"] == 0
    serve_metrics.record_gather_started()
    try:
        _, detail = mcp.serve._readiness()
        assert detail["gathers_inflight"] == 1
    finally:
        serve_metrics.reset()


def test_health_route_includes_gathers_inflight(isolated_home):
    serve_metrics.reset()
    serve_metrics.record_gather_started()
    try:
        body = TestClient(mcp.serve._http_app(_FakeServer())).get("/health").json()
        assert body["gathers_inflight"] == 1
    finally:
        serve_metrics.reset()


def test_health_route_includes_freshness(isolated_home):
    _seed_corpus(isolated_home, "tls", "2025-01-01T00:00:00Z")
    client = TestClient(mcp.serve._http_app(_FakeServer()))
    body = client.get("/health").json()
    assert body["version"] == __version__
    assert body["corpora"]["count"] == 1
    assert body["corpora"]["oldest"]["corpus"] == "tls"


# --- R18 on the cloud backend: freshness must never trigger a blob fetch ---


def test_corpora_freshness_never_materialises_on_cloud_backend(
    tmp_path, monkeypatch
):
    """`/health` and `/metrics` read `last-gathered` for every cached corpus
    (R18: no upstream call). On the cloud backend, resolving that sentinel
    through the wrong accessor (`local_corpus_dir`, which materialises a
    version's full blob set onto scratch on a miss) would turn a readiness
    probe into a per-corpus S3 download. This locks the fix: a published-but-
    unstaged corpus is reported as untracked (no sentinel value available
    yet on this replica), and — the actual regression guard — nothing lands
    on local scratch as a side effect of asking."""
    from ietf_llm.store.blobs import FileBlobStore
    from ietf_llm.store.cloud import CloudCorpusStore
    from ietf_llm.store.control import KvControlPlane
    from ietf_llm.store.kv import InMemoryKvStore

    control = KvControlPlane(InMemoryKvStore())
    blobs = FileBlobStore(str(tmp_path / "bucket"))
    scratch = tmp_path / "scratch"
    store = CloudCorpusStore(control, blobs, str(scratch))

    ws = tmp_path / "ws"
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "charter.txt").write_text("hi")
    (ws / "last-gathered").write_text("2026-06-04T00:00:00Z")
    store.publish("tls", str(ws), version="v1")
    assert not scratch.exists()  # publish uploads; it never stages locally

    monkeypatch.setattr(mcp.common, "get_corpus_store", lambda: store)
    monkeypatch.setattr("ietf_llm.store.corpus.get_corpus_store", lambda: store)

    summary = mcp.serve._corpora_freshness()
    assert summary["count"] == 1
    assert summary["tracked"] == 0  # not staged here yet -- degrades cleanly
    # The regression this guards against: no fetch happened just to answer.
    assert not scratch.exists()


# --- the probes block on store reads (S3 on the cloud backend): off the loop ---


def _runs_on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_health_runs_readiness_off_the_event_loop(isolated_home, monkeypatch):
    seen: list[bool] = []
    real = mcp.serve._readiness

    def _spy() -> Any:
        seen.append(_runs_on_loop())
        return real()

    monkeypatch.setattr(mcp.serve, "_readiness", _spy)
    TestClient(mcp.serve._http_app(_FakeServer())).get("/health")
    assert seen == [False]


def test_metrics_reads_corpus_ages_off_the_event_loop(isolated_home, monkeypatch):
    seen: list[bool] = []
    real = mcp.serve._corpus_ages

    def _spy() -> Any:
        seen.append(_runs_on_loop())
        return real()

    monkeypatch.setattr(mcp.serve, "_corpus_ages", _spy)
    resp = TestClient(mcp.serve._http_app(_FakeServer())).get("/metrics")
    assert resp.status_code == 200
    assert seen == [False]


def test_health_body_does_not_disclose_the_index_path(isolated_home):
    body = TestClient(mcp.serve._http_app(_FakeServer())).get("/health").json()
    assert "index_dir" not in body
    assert body["index_dir_usable"] is True


def test_freshness_sweep_is_shared_between_probes(isolated_home, monkeypatch):
    """/health and /metrics both sweep; one sweep serves them for a few
    seconds rather than listing the store on every probe."""
    _seed_corpus(isolated_home, "tls", "2025-01-01T00:00:00Z")
    calls: list[int] = []
    real = mcp.serve._list_wgs

    def _counting() -> Any:
        calls.append(1)
        return real()

    monkeypatch.setattr(mcp.serve, "_list_wgs", _counting)
    mcp.serve._corpora_freshness()
    mcp.serve._corpora_freshness()
    assert [name for name, _ in mcp.serve._corpus_ages()] == ["tls"]
    assert len(calls) == 1


def test_freshness_sweep_refreshes_after_the_ttl(isolated_home, monkeypatch):
    _seed_corpus(isolated_home, "tls", "2025-01-01T00:00:00Z")
    assert mcp.serve._corpora_freshness()["count"] == 1
    _seed_corpus(isolated_home, "quic", "2025-01-01T00:00:00Z")
    assert mcp.serve._corpora_freshness()["count"] == 1  # still cached
    monkeypatch.setattr(mcp.serve, "_SWEEP_TTL", 0.0)
    assert mcp.serve._corpora_freshness()["count"] == 2
