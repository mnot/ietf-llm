"""HTTP transport for gather-side fetches.

A process-wide pooled, retrying `requests.Session`; a per-host-governed GET
(`governed_get`), the metered `fetch_resource`, and HTML→text cleaning
(`clean_html`). Sits low in the import graph: it depends only on
`http_governor` (the per-host concurrency slot), `http_metrics` (egress
accounting), and `log` for logging — nothing that reaches `config`.

The gather sources and the live-lookup read path fetch through here; the
offline read path imports none of this.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, Optional

import requests
from bs4 import BeautifulSoup
from urllib3.util.retry import Retry

from .. import __version__
from ..log import LogLevel, log
from ..tls import trust_store_adapter
from . import http_metrics
from .http_governor import host_slot

# Identify the client and give upstream operators a contact path. A shared
# community service (datatracker especially) would rather reach the tool's
# author than blind-block a misbehaving User-Agent; the repo URL is that path.
DEFAULT_HEADERS = {
    "User-Agent": f"ietf-llm/{__version__} (+https://github.com/mnot/ietf-llm)"
}

_SESSION: Optional[requests.Session] = None
_NO_RETRY_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def _build_session(retry: Any) -> requests.Session:
    session = requests.Session()
    # Verifies through the OS trust store where that is available -- see
    # `tls.py` for why it is scoped here rather than injected into `ssl`.
    adapter = trust_store_adapter(pool_connections=8, pool_maxsize=8, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def http_session(*, retrying: bool = True) -> requests.Session:
    """Return a process-wide pooled `requests.Session`.

    Lazily built under a lock (the MCP runner can gather two corpora in
    separate threads); urllib3's underlying pool is thread-safe for the
    concurrent GETs the pipeline issues.

    `retrying=True` (the default, and what every gather fetch wants) mounts
    the retrying adapter: a gather is a background job, so riding out a blip
    beats failing a stage. `retrying=False` returns a separate session whose
    adapter never retries — for a caller who is *waiting* on the result and
    has a correct answer to fall back on. See `governed_get`.
    """
    global _SESSION, _NO_RETRY_SESSION  # pylint: disable=global-statement
    if retrying:
        if _SESSION is None:
            with _SESSION_LOCK:
                if _SESSION is None:
                    _SESSION = _build_session(
                        Retry(
                            total=3,
                            backoff_factor=0.5,
                            status_forcelist=(429, 500, 502, 503, 504),
                            allowed_methods=frozenset({"GET", "HEAD"}),
                            respect_retry_after_header=True,
                            raise_on_status=False,
                        )
                    )
        return _SESSION
    if _NO_RETRY_SESSION is None:
        with _SESSION_LOCK:
            if _NO_RETRY_SESSION is None:
                _NO_RETRY_SESSION = _build_session(0)
    return _NO_RETRY_SESSION


def governed_get(
    url: str, *, retrying: bool = True, **kwargs: Any
) -> requests.Response:
    """GET `url` through the shared session while holding a per-host
    concurrency slot (see `http_governor`).

    Every gather-side fetch routes through here — `fetch_resource` and the
    direct `http_session().get` call sites in `gather/*` — so that a wide
    fan-out or several concurrent gathers can never exceed the per-host budget,
    datatracker especially. The GET is non-streaming, so the slot is held for
    the whole request including the body transfer — which is what bounds
    concurrency through large draft / RFC downloads. Callers handle status and
    metrics exactly as for a bare session GET.

    **`timeout` is not a deadline.** requests applies it per connect and per
    read, and the default adapter retries (`total=3`, `respect_retry_after_
    header=True`), so a host answering `429 Retry-After: 30` makes a
    `timeout=5` call take ~90s of server-dictated sleep. That is fine for a
    gather and wrong for a read with someone waiting on it, which is what
    `retrying=False` is for: it selects the non-retrying session, so the call
    is bounded by the connect + read timeouts (plus any wait for the per-host
    slot). Pass it when a failed fetch has a correct fallback and latency
    matters more than riding out a blip.
    """
    with host_slot(url):
        return http_session(retrying=retrying).get(url, **kwargs)


def http_error_detail(err: Exception) -> str:
    """`str(err)`, plus the Cloudflare Ray ID when the response carries one.

    IETF properties (datatracker especially) sit behind Cloudflare, whose bot
    protection sometimes blocks a fetch; their support needs the `cf-ray`
    header value to trace a specific block. Safe on any exception — no
    response (or no header) means no suffix.
    """
    headers = getattr(getattr(err, "response", None), "headers", None)
    ray_id = headers.get("cf-ray") if headers is not None else None
    return f"{err} (Cloudflare Ray ID: {ray_id})" if ray_id else str(err)


def fetch_resource(
    url: str, headers: Optional[Dict[str, str]] = None
) -> Optional[requests.Response]:
    """Fetch a resource and return the response object."""
    combined_headers = DEFAULT_HEADERS.copy()
    if headers:
        combined_headers.update(headers)
    try:
        res = governed_get(url, headers=combined_headers, timeout=30)
        res.raise_for_status()
        http_metrics.record(url, res.status_code, len(res.content))
        return res
    except requests.RequestException as err:
        status = err.response.status_code if err.response is not None else 0
        n_bytes = len(err.response.content) if err.response is not None else 0
        http_metrics.record(url, status, n_bytes, error=True)
        log(f"Error fetching {url}: {http_error_detail(err)}", level=LogLevel.ERROR)
        return None


def clean_html(html_content: str) -> str:
    """Simple HTML to text conversion using BeautifulSoup with aggressive cleaning."""
    if not html_content:
        return ""
    bs_soup = BeautifulSoup(html_content, "html.parser")

    # Remove common navigation and header/footer tags
    for element in bs_soup(["script", "style", "nav", "header", "footer", "aside"]):
        element.decompose()

    # Strip specific navigation and alert components
    for cls_name in ["navbar", "alert", "modal", "visually-hidden"]:

        def match_class(cls_val: Optional[str], target: str = cls_name) -> bool:
            return bool(
                cls_val and any(val.startswith(target) for val in cls_val.split())
            )

        for element in bs_soup.find_all(class_=match_class):
            if element.name not in ["body", "html", "main"]:
                element.decompose()

    # Specifically remove the "Skip to main content" links
    for skip_link in bs_soup.find_all("a"):
        skip_text = skip_link.get_text(strip=True).lower()
        if "skip to" in skip_text:
            skip_link.decompose()

    # Get text
    text = bs_soup.get_text()

    # Break into lines and remove leading and trailing space on each
    lines = (line.strip() for line in text.splitlines())

    # Prohibited patterns (mostly IETF boilerplate/footer links)
    prohibited = [
        r"^Privacy Statement$",
        r"^About IETF Datatracker$",
        r"^Version \d",
        r"^System Status$",
        r"^Report a bug$",
        r"^IETF LLC$",
        r"^IETF Trust$",
        r"^RFC Editor$",
        r"^IANA$",
        r"^NomComs$",
        r"^Downref registry$",
        r"^Liaison statements$",
    ]
    prohibited_regex = re.compile("|".join(prohibited), re.I)

    # Filter out lines that match prohibited patterns or are empty
    filtered_lines = []
    for line in lines:
        if not line:
            continue
        if prohibited_regex.match(line):
            continue
        filtered_lines.append(line)

    # Reassemble and drop blank lines
    text = "\n".join(filtered_lines)

    return text.strip()
