"""Per-gather HTTP egress metrics — the gather/write side only.

`gather` is the only part of ietf-llm that touches the network, and a
single `ietf-llm <wg>` fans out into dozens of requests to datatracker
(drafts / RFCs / ballots / charter / meetings / people), mailarchive,
GitHub, and draft hosts. This module gives us visibility into how much
upstream load we generate: a passive accumulator that the two network
chokepoints record every request into —

  - `_get_json` in `gather/datatracker.py` (the ETag-aware JSON API path)
  - `fetch_resource` in `utils.py` (raw resource fetches)

— which together cover essentially all egress. It counts requests split
by outcome (real transfer / 304 revalidation / error), total bytes
pulled down, a per-host breakdown, and a top-N of URL patterns to spot
hot loops.

The current accumulator is **thread-local**: the MCP gather runner runs
each corpus in its own daemon thread and can gather two corpora at once,
so a process-global counter would cross-contaminate. The sequential
`ietf-llm --all` path resets per corpus in the one thread. The CLI
prints `summary_line()` at the end of a run and `persist()` stashes the
same numbers in `<wg>/gather-metrics.json` (next to the `last-gathered`
sentinel, one level above `files/`, so it's neither indexed nor
exported) for cross-run comparison.

Zero new dependencies; measurement first. Once we have a baseline we can
decide whether throttling or longer cache TTLs are warranted.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict
from urllib.parse import urlsplit

#: How many URL patterns to surface in the summary / persisted file.
_TOP_N = 8

_METRICS_NAME = "gather-metrics.json"

_DIGITS = re.compile(r"\d+")


def url_pattern(url: str) -> str:
    """Collapse a URL to a coarse pattern for hot-loop detection.

    Host + path with runs of digits collapsed to `#`, plus the sorted
    query-parameter *names* (values dropped). So every paginated
    `.../doc/document/?group__acronym=X&limit=200&type=draft` request
    folds onto one pattern regardless of which group or page offset, and
    a ballooning per-document walk shows up as a single high-count line.
    """
    parts = urlsplit(url)
    path = _DIGITS.sub("#", parts.path)
    keys = sorted({kv.split("=", 1)[0] for kv in parts.query.split("&") if kv})
    base = f"{parts.netloc}{path}"
    return f"{base}?{'&'.join(keys)}" if keys else base


@dataclass
class HttpMetrics:
    """Accumulated egress for one gather run."""

    ok: int = 0  # 2xx with a real body transfer
    revalidated: int = 0  # 304 Not Modified (ETag cache hit)
    errors: int = 0  # non-2xx response or transport failure
    bytes_received: int = 0
    host_requests: "Counter[str]" = field(default_factory=Counter)
    host_bytes: "Counter[str]" = field(default_factory=Counter)
    patterns: "Counter[str]" = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return self.ok + self.revalidated + self.errors

    def record(
        self,
        url: str,
        status_code: int,
        n_bytes: int,
        *,
        error: bool = False,
    ) -> None:
        """Record one HTTP attempt. `error=True` marks a transport failure
        or non-2xx; otherwise `status_code` 304 counts as a revalidation
        and anything else as a real transfer."""
        host = urlsplit(url).netloc or "?"
        self.host_requests[host] += 1
        if error:
            self.errors += 1
        elif status_code == 304:
            self.revalidated += 1
        else:
            self.ok += 1
        n_bytes = max(0, int(n_bytes or 0))
        self.bytes_received += n_bytes
        self.host_bytes[host] += n_bytes
        self.patterns[url_pattern(url)] += 1

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable snapshot for `<wg>/gather-metrics.json`."""
        return {
            "requests": self.total,
            "ok": self.ok,
            "revalidated": self.revalidated,
            "errors": self.errors,
            "bytes_received": self.bytes_received,
            "by_host": {
                host: {
                    "requests": count,
                    "bytes": self.host_bytes.get(host, 0),
                }
                for host, count in self.host_requests.most_common()
            },
            "top_patterns": [
                {"pattern": pat, "requests": count}
                for pat, count in self.patterns.most_common(_TOP_N)
            ],
        }

    def summary_line(self) -> str:
        """One-line human summary for the end of a gather run."""
        if self.total == 0:
            return "Network: no upstream requests."
        hosts = ", ".join(
            f"{host} {count}" for host, count in self.host_requests.most_common()
        )
        return (
            f"Network: {self.total} requests "
            f"({self.ok} transferred, {self.revalidated} revalidated, "
            f"{self.errors} errors), {_humanize_bytes(self.bytes_received)} "
            f"received [{hosts}]."
        )


def _humanize_bytes(num_bytes: int) -> str:
    """`512 B` / `4.0 KB` / `1.8 MB` — base-1000, matching curl/wget."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} GB"  # pragma: no cover - unreachable, loop returns first


# --- Thread-local current accumulator --------------------------------------

_state = threading.local()


def reset() -> HttpMetrics:
    """Start a fresh accumulator for the current thread and return it.

    Called at the top of each gather so `--all` (sequential, one thread)
    reports per-corpus numbers rather than a running total."""
    metrics = HttpMetrics()
    _state.metrics = metrics
    return metrics


def current() -> HttpMetrics:
    """The current thread's accumulator, lazily created.

    A request recorded outside an explicit `reset()` (e.g. a stray
    metadata lookup before the pipeline starts) still lands somewhere
    rather than raising."""
    metrics = getattr(_state, "metrics", None)
    if metrics is None:
        metrics = HttpMetrics()
        _state.metrics = metrics
    return metrics


def record(
    url: str,
    status_code: int,
    n_bytes: int,
    *,
    error: bool = False,
) -> None:
    """Record one HTTP attempt into the current thread's accumulator."""
    current().record(url, status_code, n_bytes, error=error)


def persist(wg_cache_dir: str) -> None:
    """Write the current accumulator to `<wg_cache_dir>/gather-metrics.json`.

    The caller passes the corpus directory (`<cache>/<wg>/`) so this module
    needs nothing from `utils`, which imports us back (it hosts one of the
    chokepoints). Best-effort, like the freshness sentinel it sits beside:
    a failed write just means there's no baseline to compare next run."""
    path = os.path.join(wg_cache_dir, _METRICS_NAME)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(current().to_dict(), fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
