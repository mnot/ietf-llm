"""Mirror the RFC-series index from rfc.fyi into the local cache.

rfc.fyi already publishes the canonical, edge-cached JSON for the whole
RFC series (`rfcs.json`, `refs.json`, `tags.json`), rebuilt from
`rfc-index.xml` + the reference graph + curated collections. Rather than
re-run that pipeline we mirror its output into
`~/.cache/ietf-llm/_rfc/`, where `rfcs.RfcData` reads it network-free.

This is a singleton, not a per-corpus artifact: `ensure_rfc_index` runs
once per `ietf-llm` invocation (see `cli.main.main`), invisibly. It is
cheap to call:

  - **TTL guard.** If the local copy is younger than `RFC_TTL_SECONDS`
    we don't touch the network at all.
  - **Conditional GET.** Past the TTL we revalidate with `If-None-Match`
    from a `.etag` sidecar; an unchanged file comes back 304 with no
    body, and we just touch the local file's mtime to restart the TTL.
  - **gzip** is negotiated automatically by `requests` (we don't set
    `Accept-Encoding`), so a real 200 transfers compressed.

Sidecar hygiene: the body is written first (atomic temp+rename), then
the `.etag` — so an etag never points at a body we failed to write. On a
200 with no ETag header we delete any stale sidecar, keeping the pair
consistent. The reader ignores sidecars entirely; the `.json` is the
source of truth.

Best-effort throughout: any network or write failure leaves the existing
cache in place and logs at PROGRESS. A missing index is not an error —
the reader degrades to a "not gathered yet" message.
"""

from __future__ import annotations

import os

import requests

from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get
from ...singletons.rfcs import RFC_FILES, rfc_index_dir
from . import _mirror

RFC_DATA_BASE = "https://rfc.fyi/var"

#: Revalidate at most once per day. The series changes slowly and the
#: 304 path is cheap, so this is just to avoid a needless request on
#: back-to-back gathers (notably `ietf-llm --all`).
RFC_TTL_SECONDS = 24 * 60 * 60

_TIMEOUT = 30


def ensure_rfc_index(
    verbosity: Verbosity = Verbosity.STATUS, force: bool = False
) -> None:
    """Refresh the local RFC index mirror if stale. Never raises."""
    target_dir = rfc_index_dir()
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as err:
        log(
            f"RFC index: cannot create {target_dir}: {err}",
            verbosity,
            LogLevel.PROGRESS,
        )
        return
    for name in RFC_FILES:
        _refresh_one(target_dir, name, verbosity, force)


def _refresh_one(target_dir: str, name: str, verbosity: Verbosity, force: bool) -> None:
    body_path = os.path.join(target_dir, name)
    etag_path = body_path + ".etag"
    if not force and _mirror.is_fresh(body_path, RFC_TTL_SECONDS):
        return
    headers = dict(DEFAULT_HEADERS)
    etag = _mirror.read_etag(etag_path) if os.path.exists(body_path) else None
    if etag:
        headers["If-None-Match"] = etag
    url = f"{RFC_DATA_BASE}/{name}"
    try:
        response = governed_get(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if response.status_code == 304:
        _mirror.touch(body_path)
        return
    try:
        response.raise_for_status()
    except requests.RequestException as err:
        log(f"RFC index: fetch {name} failed: {err}", verbosity, LogLevel.PROGRESS)
        return
    if not _mirror.write_body(body_path, response.content, verbosity, "RFC index"):
        return
    _mirror.write_sidecar(etag_path, response.headers.get("ETag"))
    log(f"RFC index: updated {name}", verbosity, LogLevel.PROGRESS)
