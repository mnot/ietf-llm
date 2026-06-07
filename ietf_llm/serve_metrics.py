"""Serve-side RED metrics for the hosted HTTP deployment — the read side.

Distinct from `http_metrics.py` (gather/write-side egress accounting): this
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
by the endpoint, so this module stays free of any `mcp_server` import
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
from typing import Dict, Iterable, List, Optional, Tuple

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

    __slots__ = ("counts", "sum", "count", "errors", "timeouts")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counts: List[int] = [0] * len(_BUCKETS)
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
        for i, bound in enumerate(_BUCKETS):
            if value <= bound:
                self.counts[i] += 1


# --- Process-global registry -----------------------------------------------

_LOCK = threading.Lock()
#: tool name -> its RED histogram (carries request count + errors + latency)
_tools: Dict[str, _Histogram] = {}
#: the remote /embeddings backend (a single unlabelled series)
_embed = _Histogram()
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

    Called from `_offload` in `mcp_server.py` for every tool, on the way
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


def adjust_inflight(delta: int) -> None:
    """Bump the in-flight tool-call gauge by `delta` (+1 entering `_offload`,
    -1 in its `finally`)."""
    with _LOCK:
        _inflight[0] += delta


def reset() -> None:
    """Clear all counters. For tests; the server never calls this."""
    with _LOCK:
        _tools.clear()
        _embed.reset()
        _inflight[0] = 0


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
    for bound, count in zip(_BUCKETS, hist.counts):
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
    here so this module needs nothing from `mcp_server` / `freshness`.
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
