"""Gather-side HTTP egress: transport, per-host concurrency, egress metrics.

The write path's networking, grouped so the top-level tree isn't littered with
it. The offline read path imports none of this (the one read exception,
`live_lookup`, fetches through `transport`):

  transport.py      — the pooled retrying session + governed_get / fetch_resource
                      / clean_html (the fetch functions, re-exported below)
  http_governor.py  — per-host concurrency slots (datatracker kept tight)
  http_metrics.py   — per-gather egress accounting (gather-metrics.json)

`transport`'s fetch functions are re-exported here, so `from ..net import
governed_get` keeps working. `http_governor` / `http_metrics` are reached as
submodules (`from ..net import http_metrics`; `http_metrics.record(...)`),
keeping their module namespace at call sites.
"""

from __future__ import annotations

from .transport import (
    DEFAULT_HEADERS,
    clean_html,
    fetch_resource,
    governed_get,
    http_session,
)

__all__ = [
    "DEFAULT_HEADERS",
    "clean_html",
    "fetch_resource",
    "governed_get",
    "http_session",
]
