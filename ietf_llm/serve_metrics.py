"""Serve-side RED metrics for the hosted HTTP deployment — the read side.

Distinct from `net/http_metrics.py` (gather/write-side egress accounting): this
module is the process-global, in-memory registry behind the `GET /metrics`
route the HTTP serve path exposes (R8). It records, for the running server:

  - **RED per tool** — every `_offload`-wrapped MCP tool records its
    request count, error count, and a latency histogram, labelled by tool
    (issue #40).
  - **Embed backend** — each request to the remote OpenAI-compatible
    `/embeddings` endpoint (`embeddings/models.py`) records call count,
    error count, and latency. This is the one read-path dependency that
    drives a paid, metered upstream, so it is the thing most worth
    watching for cost and latency.

Index-freshness gauges are NOT held here: they are derived at scrape time
from the per-corpus `last-gathered` sentinels and passed into `render()`
by the endpoint, so this module stays free of any `ietf_llm.mcp` import
(no cycle) and holds no state that can go stale between scrapes.

Zero new dependencies: the Prometheus text exposition format (v0.0.4) is
simple enough to emit by hand, so the stdio/local install stays lean and
nothing new is pulled onto the serve image either. The registry is
process-global behind a lock — unlike the thread-local gather accumulator,
a served process wants the aggregate across all its request threads.

Recording is unconditional (a handful of updates under a lock, paid on
every tool call regardless of transport); the data is only ever exposed
when the HTTP `/metrics` route is mounted, so the stdio path accumulates
harmless counters no one reads.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

#: Histogram bucket upper bounds in seconds (the implicit +Inf bucket is
#: emitted last). Spans a sub-10ms cache hit through a multi-second
#: embedding-model cold load up to the tool deadline. The top bounds (60,
#: 120) reach the default `IETF_LLM_TOOL_TIMEOUT` (120s): without them a
#: call crawling toward its deadline — or a slow remote embedding call,
#: which over a network easily exceeds 30s — would land in +Inf and the
#: histogram would lose all resolution in exactly the range that matters
#: when something is going wrong.
_BUCKETS: Tuple[float, ...] = (
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)

#: Coarser bucket bounds (seconds) for in-session gather duration: a gather is
#: a minutes-long background job, not a request, so it needs a range reaching
#: the ~hour a large cold gather can take — the per-call buckets above would
#: pin every gather in +Inf.
_GATHER_BUCKETS: Tuple[float, ...] = (
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1200.0,
    1800.0,
    3600.0,
)


class _Histogram:
    """A minimal Prometheus-style histogram plus error/timeout counters.

    `counts[i]` is the cumulative "<= _BUCKETS[i]" observation count, so
    each bucket renders directly. `errors` and `timeouts` ride along here
    rather than in a parallel map so a tool's RED triplet
    (requests/errors/latency) lives in one object. A timeout counts as an
    error *and* as a timeout: `errors` stays the inclusive failure total
    (don't subtract), while `timeouts` isolates deadline hits from raised
    exceptions — they point at different causes (a slow upstream / cold
    embedding load / cache contention vs. a bug)."""

    __slots__ = ("buckets", "counts", "sum", "count", "errors", "timeouts")

    def __init__(self, buckets: Tuple[float, ...] = _BUCKETS) -> None:
        self.buckets = buckets
        self.reset()

    def reset(self) -> None:
        self.counts: List[int] = [0] * len(self.buckets)
        self.sum: float = 0.0
        self.count: int = 0
        self.errors: int = 0
        self.timeouts: int = 0

    def observe(self, value: float, *, error: bool, timeout: bool = False) -> None:
        self.count += 1
        self.sum += value
        if error:
            self.errors += 1
        if timeout:
            self.timeouts += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1


# --- Process-global registry -----------------------------------------------

_LOCK = threading.Lock()
#: tool name -> its RED histogram (carries request count + errors + latency)
_tools: Dict[str, _Histogram] = {}
#: corpus-store operation -> its RED histogram. The CorpusStore seam is the
#: read path's other I/O dependency: on the cloud backend, resolving a version
#: pointer or materialising a corpus' files/index hits object storage, which
#: the per-tool latency absorbs but cannot attribute. On the local backend
#: these are cheap stat() calls and the series are near-idle — harmless.
_store: Dict[str, _Histogram] = {}
#: the remote /embeddings backend (a single unlabelled series)
_embed = _Histogram()
#: in-session gather lifecycle (see `mcp.common._gather_enabled`). The gather
#: is the server's one write+network path and runs for minutes in the
#: background, so its liveness is invisible to the per-tool RED above. We track
#: how many run concurrently, how many have started, the terminal outcomes by
#: state, and a duration histogram. Near-idle when gather is disabled.
_gather_inflight = [0]
_gather_started = [0]
#: terminal state ("done" | "failed" | "cancelled") -> count
_gather_outcomes: Dict[str, int] = {}
_gather_duration = _Histogram(_GATHER_BUCKETS)
#: tool calls currently executing in `_offload` (the saturation "U" the RED
#: latency histograms can't show on their own): incremented on the way in,
#: decremented in the `finally`, so a scrape sees concurrent in-flight work.
#: A one-element list so it is mutated in place under the lock (like `_embed`),
#: not rebound — no module-level `global`.
_inflight = [0]


def record_tool(
    tool: str, elapsed: float, *, error: bool, timeout: bool = False
) -> None:
    """Record one MCP tool invocation: its latency and whether it errored.

    Called from `_offload` in `mcp/common.py` for every tool, on the way
    out (the `finally`), so a timeout or exception is still counted. A
    deadline hit passes `timeout=True` (and `error=True`): it lands in
    both the errors total and the separate timeouts total."""
    with _LOCK:
        hist = _tools.get(tool)
        if hist is None:
            hist = _Histogram()
            _tools[tool] = hist
        hist.observe(elapsed, error=error, timeout=timeout)


def record_embed(elapsed: float, *, error: bool) -> None:
    """Record one request to the remote `/embeddings` endpoint."""
    with _LOCK:
        _embed.observe(elapsed, error=error)


def record_store(op: str, elapsed: float, *, error: bool) -> None:
    """Record one corpus-store read operation (`op` is its method name)."""
    with _LOCK:
        hist = _store.get(op)
        if hist is None:
            hist = _Histogram()
            _store[op] = hist
        hist.observe(elapsed, error=error)


_T = TypeVar("_T")


def timed_store(op: str, fn: Callable[[], _T]) -> _T:
    """Run a corpus-store read `fn`, recording its latency under `op` and
    whether it raised — so the serve read boundary times its store calls
    without each call site repeating the try/finally."""
    start = time.monotonic()
    errored = True
    try:
        result = fn()
        errored = False
        return result
    finally:
        record_store(op, time.monotonic() - start, error=errored)


def record_gather_started() -> None:
    """One in-session gather entered the `running` state."""
    with _LOCK:
        _gather_started[0] += 1
        _gather_inflight[0] += 1


def record_gather_finished(state: str, duration: float) -> None:
    """One in-session gather reached a terminal state (`done` / `failed` /
    `cancelled`) after `duration` seconds running. Balances the in-flight
    gauge bumped by `record_gather_started`."""
    with _LOCK:
        if _gather_inflight[0] > 0:
            _gather_inflight[0] -= 1
        _gather_outcomes[state] = _gather_outcomes.get(state, 0) + 1
        _gather_duration.observe(duration, error=state == "failed")


def adjust_inflight(delta: int) -> None:
    """Bump the in-flight tool-call gauge by `delta` (+1 entering `_offload`,
    -1 in its `finally`)."""
    with _LOCK:
        _inflight[0] += delta


def gathers_inflight() -> int:
    """How many in-session gathers are running right now.

    The same gauge the `ietf_llm_gathers_inflight` Prometheus series renders,
    exposed as a plain int so the `/health` JSON can carry it without a
    `/metrics` parse. A fronting proxy keys its container-lifetime decision
    (keep alive while a background gather runs) off this — see `_readiness`."""
    with _LOCK:
        return _gather_inflight[0]


def reset() -> None:
    """Clear all counters. For tests; the server never calls this."""
    with _LOCK:
        _tools.clear()
        _store.clear()
        _embed.reset()
        _inflight[0] = 0
        _gather_inflight[0] = 0
        _gather_started[0] = 0
        _gather_outcomes.clear()
        _gather_duration.reset()


# --- Prometheus text exposition --------------------------------------------


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, double-quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value: float) -> str:
    """Render a whole-number float without its trailing `.0`, so integer
    counts read as integers in the exposition."""
    if value == int(value):
        return str(int(value))
    return repr(value)


def _emit_histogram(
    lines: List[str], name: str, label: str, hist: "_Histogram"
) -> None:
    """Append the `_bucket`/`_sum`/`_count` series for one histogram.

    `label` is a pre-rendered `key="value"` fragment (or ""). Buckets are
    emitted directly because `hist.counts` is already cumulative."""
    inner = f"{label}," if label else ""
    for bound, count in zip(hist.buckets, hist.counts):
        lines.append(f'{name}_bucket{{{inner}le="{_fmt(bound)}"}} {count}')
    lines.append(f'{name}_bucket{{{inner}le="+Inf"}} {hist.count}')
    suffix = f"{{{label}}}" if label else ""
    lines.append(f"{name}_sum{suffix} {_fmt(hist.sum)}")
    lines.append(f"{name}_count{suffix} {hist.count}")


def render(
    corpus_ages: Optional[Iterable[Tuple[str, int]]] = None,
    *,
    version: Optional[str] = None,
) -> str:
    """Render the full Prometheus text exposition.

    `corpus_ages` is `(corpus, age_seconds)` pairs the endpoint derives
    from the `last-gathered` sentinels at scrape time (only tracked
    corpora; untracked ones are omitted). Passed in rather than computed
    here so this module needs nothing from `ietf_llm.mcp` / `freshness`.
    `version` is the package version for the `build_info` series, passed in
    for the same reason (the endpoint already holds `__version__`).

    The whole body is built under the lock so a single scrape is a
    consistent snapshot; scrapes are infrequent and recorders only block
    for the few microseconds it takes to format some lines.
    """
    with _LOCK:
        return _render_locked(corpus_ages, version)


def _render_locked(
    corpus_ages: Optional[Iterable[Tuple[str, int]]], version: Optional[str]
) -> str:
    tools = sorted(_tools.items())
    lines: List[str] = []

    # Build identity (constant 1; the version rides in the label, the
    # standard Prometheus way to expose a build string for a `count by`).
    if version is not None:
        lines.append("# HELP ietf_llm_build_info Build version (value is always 1).")
        lines.append("# TYPE ietf_llm_build_info gauge")
        lines.append(f'ietf_llm_build_info{{version="{_escape_label(version)}"}} 1')

    # In-flight tool calls right now (saturation).
    lines.append("# HELP ietf_llm_inflight_requests Tool calls executing now.")
    lines.append("# TYPE ietf_llm_inflight_requests gauge")
    lines.append(f"ietf_llm_inflight_requests {_inflight[0]}")

    # RED per tool.
    lines.append("# HELP ietf_llm_tool_requests_total MCP tool invocations.")
    lines.append("# TYPE ietf_llm_tool_requests_total counter")
    for tool, hist in tools:
        lab = f'tool="{_escape_label(tool)}"'
        lines.append(f"ietf_llm_tool_requests_total{{{lab}}} {hist.count}")

    lines.append("# HELP ietf_llm_tool_errors_total Errored/timed-out tool calls.")
    lines.append("# TYPE ietf_llm_tool_errors_total counter")
    for tool, hist in tools:
        lab = f'tool="{_escape_label(tool)}"'
        lines.append(f"ietf_llm_tool_errors_total{{{lab}}} {hist.errors}")

    lines.append(
        "# HELP ietf_llm_tool_timeouts_total Tool calls killed by the deadline "
        "(a subset of errors)."
    )
    lines.append("# TYPE ietf_llm_tool_timeouts_total counter")
    for tool, hist in tools:
        lab = f'tool="{_escape_label(tool)}"'
        lines.append(f"ietf_llm_tool_timeouts_total{{{lab}}} {hist.timeouts}")

    lines.append("# HELP ietf_llm_tool_latency_seconds MCP tool latency.")
    lines.append("# TYPE ietf_llm_tool_latency_seconds histogram")
    for tool, hist in tools:
        lab = f'tool="{_escape_label(tool)}"'
        _emit_histogram(lines, "ietf_llm_tool_latency_seconds", lab, hist)

    # Embed backend (remote /embeddings).
    lines.append("# HELP ietf_llm_embed_requests_total Remote /embeddings requests.")
    lines.append("# TYPE ietf_llm_embed_requests_total counter")
    lines.append(f"ietf_llm_embed_requests_total {_embed.count}")
    lines.append("# HELP ietf_llm_embed_errors_total Failed /embeddings requests.")
    lines.append("# TYPE ietf_llm_embed_errors_total counter")
    lines.append(f"ietf_llm_embed_errors_total {_embed.errors}")
    lines.append("# HELP ietf_llm_embed_latency_seconds Remote /embeddings latency.")
    lines.append("# TYPE ietf_llm_embed_latency_seconds histogram")
    _emit_histogram(lines, "ietf_llm_embed_latency_seconds", "", _embed)

    # Corpus-store RED (the cloud backend's object-store reads; near-idle on
    # the local backend). Labelled by operation, like the per-tool series.
    store = sorted(_store.items())
    lines.append("# HELP ietf_llm_store_requests_total Corpus-store read ops.")
    lines.append("# TYPE ietf_llm_store_requests_total counter")
    for op, hist in store:
        lab = f'op="{_escape_label(op)}"'
        lines.append(f"ietf_llm_store_requests_total{{{lab}}} {hist.count}")
    lines.append("# HELP ietf_llm_store_errors_total Failed corpus-store reads.")
    lines.append("# TYPE ietf_llm_store_errors_total counter")
    for op, hist in store:
        lab = f'op="{_escape_label(op)}"'
        lines.append(f"ietf_llm_store_errors_total{{{lab}}} {hist.errors}")
    lines.append("# HELP ietf_llm_store_latency_seconds Corpus-store read latency.")
    lines.append("# TYPE ietf_llm_store_latency_seconds histogram")
    for op, hist in store:
        lab = f'op="{_escape_label(op)}"'
        _emit_histogram(lines, "ietf_llm_store_latency_seconds", lab, hist)

    # In-session gather lifecycle (opt-in; near-idle when disabled).
    lines.append("# HELP ietf_llm_gathers_inflight Gathers running now.")
    lines.append("# TYPE ietf_llm_gathers_inflight gauge")
    lines.append(f"ietf_llm_gathers_inflight {_gather_inflight[0]}")
    lines.append("# HELP ietf_llm_gathers_started_total Gathers that began running.")
    lines.append("# TYPE ietf_llm_gathers_started_total counter")
    lines.append(f"ietf_llm_gathers_started_total {_gather_started[0]}")
    lines.append("# HELP ietf_llm_gathers_total Gathers that reached a terminal state.")
    lines.append("# TYPE ietf_llm_gathers_total counter")
    for state, outcome_count in sorted(_gather_outcomes.items()):
        lab = f'state="{_escape_label(state)}"'
        lines.append(f"ietf_llm_gathers_total{{{lab}}} {outcome_count}")
    lines.append("# HELP ietf_llm_gather_duration_seconds Completed-gather duration.")
    lines.append("# TYPE ietf_llm_gather_duration_seconds histogram")
    _emit_histogram(lines, "ietf_llm_gather_duration_seconds", "", _gather_duration)

    # Index freshness, computed by the caller from the last-gathered
    # sentinels (no upstream call; R18).
    lines.append(
        "# HELP ietf_llm_corpus_last_gathered_age_seconds "
        "Per-corpus last-gathered age."
    )
    lines.append("# TYPE ietf_llm_corpus_last_gathered_age_seconds gauge")
    for corpus, age in corpus_ages or ():
        lab = f'corpus="{_escape_label(corpus)}"'
        lines.append(f"ietf_llm_corpus_last_gathered_age_seconds{{{lab}}} {int(age)}")

    return "\n".join(lines) + "\n"
