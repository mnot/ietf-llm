"""Per-host concurrency governor for gather HTTP egress.

`gather` is the only part of ietf-llm that touches the network, and a single
run fans out across a handful of hosts: datatracker (the JSON metadata API),
the draft / RFC text hosts (`www.ietf.org`, `www.rfc-editor.org`), GitHub, and
the RFC / catalog mirrors. Historically the pipeline was strictly serial, so
upstream load was self-limiting — the implicit rate limit was "one request at a
time, and at most `gather_max_inflight` gathers at once". The moment any stage
fans out concurrently (the document-text downloads do) that guarantee is gone,
and several gathers already share one process (see `gather_runner`).

This module restores the bound *explicitly* and independently of how the
pipeline is structured: every governed request holds one of a small per-host
pool of slots, so no matter how wide a fan-out or how many gathers share the
process, a given host sees at most N requests in flight at once. `datatracker`
gets the tightest budget on purpose — it is a shared community service backed
by a database, where the draft / RFC text hosts are CDN-fronted static files
that tolerate more. The caps are the keystone that lets the document stage
parallelise (fast) while keeping datatracker polite (safe).

Deliberately stdlib-only — it imports nothing from the rest of the package.
`utils` (which the governor wraps) sits *below* `config` in the import graph, so
anything `utils` imports must not reach the `config` package (including
`config.service`), or it forms a cycle. The two caps are therefore read straight
from the environment here rather than through `config.service`:

  - ``IETF_LLM_HTTP_MAX_PER_HOST`` (default 6) — the general per-host cap.
  - ``IETF_LLM_HTTP_MAX_DATATRACKER`` (default 2) — the tighter datatracker cap.

Both are operational knobs (env only; no config.json layer). IMAP is not
governed here: it is a single persistent connection per list, already naturally
serial.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterator
from urllib.parse import urlsplit

#: Hosts that get the tighter, datatracker-tier cap, matched by exact netloc.
#: Everything else (CDN-fronted static text, GitHub, mirrors) uses the general
#: cap, which is looser.
_TIGHT_HOSTS = frozenset({"datatracker.ietf.org"})

_ENV_MAX_PER_HOST = "IETF_LLM_HTTP_MAX_PER_HOST"
_ENV_MAX_DATATRACKER = "IETF_LLM_HTTP_MAX_DATATRACKER"
_DEFAULT_MAX_PER_HOST = 6
_DEFAULT_MAX_DATATRACKER = 2

_lock = threading.Lock()
_sems: Dict[str, threading.BoundedSemaphore] = {}


def _env_cap(name: str, default: int) -> int:
    """Read a positive-int cap from the environment, falling back to `default`
    for unset, non-numeric, or sub-1 values."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 1 else default


def _cap_for(host: str) -> int:
    """Per-host concurrency cap, read from the environment (see module
    docstring for why this does not go through `service_config`)."""
    if host in _TIGHT_HOSTS:
        return _env_cap(_ENV_MAX_DATATRACKER, _DEFAULT_MAX_DATATRACKER)
    return _env_cap(_ENV_MAX_PER_HOST, _DEFAULT_MAX_PER_HOST)


def _sem_for(host: str) -> threading.BoundedSemaphore:
    """The shared semaphore for `host`, created once with that host's cap.

    The cap is fixed for the life of the process (config is read once, when
    the host is first seen); `reset()` clears the table for tests that vary
    it."""
    with _lock:
        sem = _sems.get(host)
        if sem is None:
            sem = threading.BoundedSemaphore(_cap_for(host))
            _sems[host] = sem
        return sem


@contextmanager
def host_slot(url: str) -> "Iterator[None]":
    """Hold a per-host concurrency slot for the body, blocking until one is
    free. Keyed on the URL's netloc, so all gather egress to one host shares a
    single pool regardless of which stage or gather thread issued it."""
    host = urlsplit(url).netloc or "?"
    sem = _sem_for(host)
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def reset() -> None:
    """Drop the cached per-host semaphores so the next `host_slot` rebuilds
    them from current config. For tests that vary the caps; not needed in
    normal operation, where config is constant for the process lifetime."""
    with _lock:
        _sems.clear()
