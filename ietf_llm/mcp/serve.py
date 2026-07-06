"""HTTP transport, /health, /metrics, and serve-time config validation."""

from __future__ import annotations

import datetime
import os
from typing import Any, Dict, List, Tuple

from .. import __version__, config, serve_metrics
from ..config import service as service_config
from ..embeddings import (
    DEFAULT_EMBED_MODEL,
    any_indexed_wg,
    is_remote_embed_model,
    probe_index,
)
from ..freshness import last_gathered
from ..paths import get_index_dir
from ..utils import LogLevel, log
from .common import _gather_enabled, _list_wgs, _log_verbosity


def _corpora_freshness() -> "dict[str, Any]":
    """Bounded freshness summary across all cached corpora (R18).

    Reads only the per-corpus `last-gathered` sentinels -- no upstream
    call -- so a replica can report how stale the data it serves is
    without touching the network. `count` is every cached corpus;
    `tracked` is the subset carrying a sentinel (caches predating
    freshness tracking, or populated out of band, have none, so they
    count but aren't tracked). `oldest` / `newest` bound the staleness
    window without a per-corpus row, keeping the payload small on a box
    serving many corpora -- the per-corpus breakdown is the /metrics
    scrape's job. Both are null when nothing is tracked.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    wgs = _list_wgs()
    tracked = [(wg, when) for wg in wgs if (when := last_gathered(wg)) is not None]

    def _entry(item: "tuple[str, datetime.datetime]") -> "dict[str, Any]":
        wg, when = item
        return {
            "corpus": wg,
            "last_gathered": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_seconds": max(0, int((now - when).total_seconds())),
        }

    summary: "dict[str, Any]" = {
        "count": len(wgs),
        "tracked": len(tracked),
        "oldest": None,
        "newest": None,
    }
    if tracked:
        summary["oldest"] = _entry(min(tracked, key=lambda it: it[1]))
        summary["newest"] = _entry(max(tracked, key=lambda it: it[1]))
    return summary


def _readiness() -> "tuple[bool, dict[str, Any]]":
    """Readiness for the container, computed WITHOUT any upstream call (R18).

    Ready when the index dir is mounted AND a real corpus index actually
    opens. Probing one index (not just stat-ing the dir) catches an index
    that is present but unservable -- e.g. a WAL-mode DB on a read-only
    mount without IETF_LLM_INDEX_IMMUTABLE, or a truncated file -- which a
    bare directory check would false-green. An empty server (no corpora
    gathered yet) is still ready: the dir is fine and there is nothing to
    open. The embedding endpoint is reported as configured-or-not but its
    reachability is deliberately NOT probed: a slow / unreachable upstream
    must not flap readiness, and R18 forbids gating liveness on an embed.
    """
    index_dir = get_index_dir()
    index_ok = os.path.isdir(index_dir) and os.access(index_dir, os.R_OK)
    probe = "skipped"
    if index_ok:
        wg = any_indexed_wg()
        if wg is None:
            probe = "no-corpora"
        else:
            probe = "ok" if probe_index(wg) else "failed"
    ready = index_ok and probe != "failed"
    return ready, {
        "version": __version__,
        "index_dir": index_dir,
        "index_dir_usable": index_ok,
        "index_probe": probe,
        "embed_endpoint_configured": bool(
            os.environ.get("IETF_LLM_EMBED_BASE_URL", "").strip()
        ),
        # In-session gathers running now. A stable JSON field so a fronting
        # proxy can decide whether to keep an idle-timing-out container alive
        # past its window (a background gather publishes nothing until it
        # finishes) without scraping/parsing the `/metrics` text format.
        "gathers_inflight": serve_metrics.gathers_inflight(),
        "corpora": _corpora_freshness(),
    }


async def _health_endpoint(_request: Any) -> Any:
    # pylint: disable=import-outside-toplevel
    from starlette.responses import JSONResponse

    ready, detail = _readiness()
    return JSONResponse(
        {"status": "ok" if ready else "unavailable", **detail},
        status_code=200 if ready else 503,
    )


def _corpus_ages() -> "List[Tuple[str, int]]":
    """Per-corpus `last-gathered` age in seconds, for the freshness gauge.

    Reads only the per-corpus sentinels (no upstream call; R18). Untracked
    corpora -- those without a sentinel -- are omitted, leaving the gauge
    to carry only ages it can actually report. This is the per-corpus
    breakdown /health deliberately summarises rather than enumerates.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ages: "List[Tuple[str, int]]" = []
    for wg in _list_wgs():
        when = last_gathered(wg)
        if when is not None:
            ages.append((wg, max(0, int((now - when).total_seconds()))))
    return ages


async def _metrics_endpoint(_request: Any) -> Any:
    # pylint: disable=import-outside-toplevel
    from starlette.responses import PlainTextResponse

    body = serve_metrics.render(_corpus_ages(), version=__version__)
    # Prometheus text exposition format v0.0.4.
    return PlainTextResponse(
        body, media_type="text/plain; version=0.0.4; charset=utf-8"
    )


def _http_app(server: Any) -> Any:
    """The Streamable HTTP ASGI app with GET /health and /metrics added.

    Both sit beside the MCP endpoint (/mcp) on the same app, so they
    share the app lifespan -- no wrapper, no lifespan propagation gotcha.
    /health is the human-glance readiness view (R18); /metrics is the
    Prometheus scrape view (issue #40). Neither makes an upstream call.
    """
    app = server.streamable_http_app()
    app.add_route("/health", _health_endpoint, methods=["GET"])
    app.add_route("/metrics", _metrics_endpoint, methods=["GET"])
    return app


def _resolve_bind() -> "Tuple[str, int]":
    """Resolve the HTTP bind host:port from the environment (defaults
    127.0.0.1:8000). A non-integer port falls back to 8000."""
    host = os.environ.get("IETF_LLM_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(os.environ.get("IETF_LLM_MCP_PORT", "8000"))
    except ValueError:
        port = 8000
    return host, port


def _csv_env(name: str) -> "List[str]":
    """A comma-separated env var as a list of stripped, non-empty items."""
    return [
        item.strip() for item in os.environ.get(name, "").split(",") if item.strip()
    ]


def _stateless_http_enabled() -> bool:
    """Whether the HTTP transport runs stateless (no per-client session).

    Default **on**: a stateless server keeps no `Mcp-Session-Id` state between
    requests, so any replica behind a load balancer can answer any request with
    no session affinity — the right shape for this read-mostly server, and what
    a horizontally-scaled deployment wants. Set `IETF_LLM_MCP_STATELESS=0`
    (or false/no/off) to restore stateful sessions. stdio ignores this.
    """
    raw = os.environ.get("IETF_LLM_MCP_STATELESS", "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def _transport_security_settings() -> "Any":
    """DNS-rebinding (Host/Origin) protection settings for the HTTP transport.

    Off by default: the server assumes a trust boundary (proxy / firewall) in
    front and does no Host validation itself (#41). This is an **explicit**
    disable, not an omission — the MCP library otherwise defaults to a
    loopback-only allow-list (`127.0.0.1:*` / `localhost:*` / `[::1]:*`), which
    silently answers a fronted public-hostname deployment with `421 Invalid Host
    header`. Returning a settings object with protection off restores the
    documented "bind wide behind a proxy" shape.

    When `IETF_LLM_MCP_ALLOWED_HOSTS` is set, enable validation and accept only
    those `Host` values — each an exact `host` / `host:port`, or a `host:*`
    wildcard that matches any port. `IETF_LLM_MCP_ALLOWED_ORIGINS` likewise
    restricts the `Origin` header (browser callers); unset means any origin.
    This lets an operator front the server directly (no proxy enforcing Host)
    without exposure to DNS-rebinding, which is otherwise the proxy's job.
    """
    from mcp.server.transport_security import (  # pylint: disable=import-outside-toplevel,import-error
        TransportSecuritySettings,
    )

    allowed_hosts = _csv_env("IETF_LLM_MCP_ALLOWED_HOSTS")
    if not allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=_csv_env("IETF_LLM_MCP_ALLOWED_ORIGINS"),
    )


def _effective_embed_model() -> str:
    """The embedding model the serve/gather paths would actually use.

    Mirrors `config.merge_global`'s precedence with no CLI in play and no
    persistence side effect: env > global-persisted > default. Read-only,
    so it is safe to call at boot to validate the embed config."""
    env = os.environ.get("IETF_LLM_EMBED_MODEL", "").strip()
    if env:
        return env
    persisted = config.load_global().get("embed_model")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip()
    return DEFAULT_EMBED_MODEL


def _effective_no_embed() -> bool:
    """Whether gather would skip embedding (env > global-persisted)."""
    env = os.environ.get("IETF_LLM_NO_EMBED", "").strip()
    if env:
        return env.lower() in ("1", "true", "yes", "on")
    return bool(config.load_global().get("no_embed", False))


def _index_immutable_enabled() -> bool:
    """Whether IETF_LLM_INDEX_IMMUTABLE is set (matches storage's predicate)."""
    return os.environ.get("IETF_LLM_INDEX_IMMUTABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


#: Prefix marking a local, torch-backed embedding model (mirrors
#: embeddings.models._ST_PREFIX). A gather that embeds with one of these
#: on a torch-free image crashes deep in the pipeline.
_LOCAL_EMBED_PREFIX = "sentence-transformers/"


def _torch_importable() -> bool:
    """True if `torch` can be imported, without importing it.

    `find_spec` only inspects the import system, so a torch-free serve
    image pays nothing and a present-but-heavy torch isn't loaded just to
    answer the question."""
    import importlib.util  # pylint: disable=import-outside-toplevel

    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _is_loopback_host(host: str) -> bool:
    """Heuristic: is `host` a loopback bind (not externally reachable)?

    String-based on the common cases rather than resolving DNS at boot
    (slow, and a resolver hiccup must not gate startup). `0.0.0.0` / `::`
    (bind-all) and any routable address or unrecognised hostname are
    treated as non-loopback -- the safe default is to assume reachable
    and warn."""
    lowered = host.strip().lower()
    return lowered in ("localhost", "::1") or lowered.startswith("127.")


def _serve_posture(host: str, port: int) -> "Dict[str, str]":
    """The always-logged boot posture: what this process is actually doing."""
    model = _effective_embed_model()
    backend = "remote" if is_remote_embed_model(model) else "local"
    allowed_hosts = _csv_env("IETF_LLM_MCP_ALLOWED_HOSTS")
    return {
        "transport": "http",
        "bind": f"{host}:{port}",
        "stateless": "yes" if _stateless_http_enabled() else "no",
        "gather": "on" if _gather_enabled() else "off",
        "embed_backend": backend,
        "embed_model": model,
        "no_embed": "yes" if _effective_no_embed() else "no",
        "index_dir": get_index_dir(),
        "index_immutable": "yes" if _index_immutable_enabled() else "no",
        "store_backend": service_config.store_backend(),
        "host_allowlist": ",".join(allowed_hosts) if allowed_hosts else "off",
        "log_level": _log_verbosity().name.lower(),
    }


def _serve_config_problems(host: str) -> "Tuple[List[str], List[str]]":
    """Cross-knob consistency check for the HTTP serve path (issue #46).

    Returns (hard_errors, warnings). Transport is not the thing to gate
    on: HTTP + in-session gather is a supported, trusted-box shape (#41).
    We refuse only configs that cannot work, and warn (not refuse) on
    exposure -- the operator owns that boundary.
    """
    errors: "List[str]" = []
    warnings: "List[str]" = []

    gather = _gather_enabled()
    model = _effective_embed_model()
    remote = is_remote_embed_model(model)

    # 1a. gather must write the index; immutable says the mount is read-only.
    if gather and _index_immutable_enabled():
        errors.append(
            "IETF_LLM_ENABLE_GATHER=1 and IETF_LLM_INDEX_IMMUTABLE=1 "
            "contradict: gather must write the index, but immutable marks "
            "the mount read-only. Unset one."
        )

    # 1b. gather-on-torch-free with a local (torch-backed) embed model would
    # crash deep in the embed step. Hard refuse (a gather-enabled server that
    # cannot gather is a misconfiguration worth surfacing now).
    if (
        gather
        and not _effective_no_embed()
        and model.startswith(_LOCAL_EMBED_PREFIX)
        and not _torch_importable()
    ):
        errors.append(
            f"IETF_LLM_ENABLE_GATHER=1 with a local embedding model "
            f"({model}) but torch is not importable: gather's embed step "
            f"would crash mid-pipeline. Set an 'openai-embed/<model>' id "
            f"(IETF_LLM_EMBED_MODEL) with IETF_LLM_EMBED_BASE_URL, install "
            f"the 'local-embeddings' extra, or set IETF_LLM_NO_EMBED=1."
        )

    # 1c. Remote embed model but no endpoint: search_corpus (the read path
    # everyone uses) would fail confusingly at request time. Independent of
    # gather.
    if remote and not os.environ.get("IETF_LLM_EMBED_BASE_URL", "").strip():
        errors.append(
            f"Embedding model {model} is remote but IETF_LLM_EMBED_BASE_URL "
            f"is not set: search_corpus would fail at request time. Set the "
            f"endpoint base URL (e.g. https://host/v1)."
        )

    # 2. Exposure without auth: warn loudly, never block. Binding wide
    # behind a proxy is the intended production shape (#41).
    if not _is_loopback_host(host):
        msg = (
            f"binding to {host} (non-loopback): the server has no "
            f"authentication or rate limiting and assumes a trust boundary "
            f"(proxy / firewall) in front (#41)."
        )
        if gather:
            msg += (
                " gather is enabled, so an unauthenticated caller could "
                "trigger cache writes and network egress."
            )
        warnings.append(msg)

    # 3. Cloud corpus store selected but under-configured: reads (and any
    # gather publish) would fail at request time. Validate the required knobs
    # are present, upfront.
    backend = service_config.store_backend()
    if backend == "cloud":
        missing = [
            env
            for env, value in (
                ("IETF_LLM_STORE_URL", service_config.store_url()),
                ("IETF_LLM_SCRATCH_DIR", service_config.scratch_dir()),
            )
            if not value
        ]
        if missing:
            errors.append(
                "IETF_LLM_STORE_BACKEND=cloud but the corpus store is "
                "under-configured: missing " + ", ".join(missing) + "."
            )
    elif backend != "local":
        errors.append(
            f"IETF_LLM_STORE_BACKEND={backend!r} is not recognised "
            "(expected 'local' or 'cloud')."
        )

    return errors, warnings


def _validate_serve_config(host: str, port: int) -> None:
    """Log the boot posture, surface warnings, and refuse on hard errors.

    Called before binding so a contradictory or under-provisioned config
    fails fast at boot rather than minutes into a gather or on the first
    search_corpus (project preference: upfront validation over
    wait-then-fail). Raises SystemExit(1) on any hard error.
    """
    posture = _serve_posture(host, port)
    log(
        "serve posture: " + " ".join(f"{k}={v}" for k, v in posture.items()),
        level=LogLevel.STATUS,
    )
    errors, warnings = _serve_config_problems(host)
    for warning in warnings:
        log(f"WARNING: {warning}", level=LogLevel.STATUS)
    if errors:
        for error in errors:
            log(f"Refusing to start: {error}", level=LogLevel.ERROR)
        raise SystemExit(1)


def _run_http(server: Any) -> None:
    """Serve the MCP server over Streamable HTTP (R8).

    Binds to IETF_LLM_MCP_HOST / IETF_LLM_MCP_PORT (defaults
    127.0.0.1:8000). FastMCP's streamable_http_app() is a standard
    MCP-spec Streamable HTTP ASGI app, so a fronting proxy can be
    near-transparent. uvicorn ships transitively with `mcp`. The custom
    threaded-writer transport is stdio-specific and does not apply here.
    """
    import uvicorn  # pylint: disable=import-outside-toplevel

    host, port = _resolve_bind()
    # Boot-time config validation + posture banner (issue #46): fail fast
    # on contradictory / under-provisioned configs, warn on exposure.
    _validate_serve_config(host, port)
    # Startup preamble: version + freshness floor, mirroring what /health
    # reports, so a rolling-deploy log shows which build a replica is on
    # and how stale its caches are at boot. Under IETF_LLM_LOG_FORMAT=json
    # this is a one-line structured record a collector can ingest.
    fresh = _corpora_freshness()
    oldest = fresh["oldest"]
    floor = (
        f"oldest {oldest['corpus']} {oldest['age_seconds'] // 86400}d"
        if oldest
        else "none tracked"
    )
    log(
        f"ietf-llm {__version__} serving HTTP on {host}:{port}; "
        f"{fresh['count']} corpora ({fresh['tracked']} tracked, {floor})",
        level=LogLevel.STATUS,
    )
    uvicorn.run(_http_app(server), host=host, port=port)
