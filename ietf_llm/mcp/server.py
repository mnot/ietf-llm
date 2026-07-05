"""Server construction: instructions, prewarm, tool registration, main()."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from importlib import resources
from typing import Any, Optional

import anyio

from .. import __version__, _debug_log, _stdio_transport
from ..embeddings import _get_embed_model, is_remote_embed_model
from ..freshness import set_deployment_mode, set_gather_default
from ..utils import Verbosity, get_index_dir, graceful_keyboard_interrupt
from . import (
    chunks,
    citations,
    corpus,
    digest,
    drafts,
    gather,
    meetings,
    norms,
    rfcs,
    search,
    topic,
)
from .common import (
    _gather_enabled,
    _resolve_transport,
    _session_section,
    _strip_frontmatter,
)
from .serve import (
    _index_immutable_enabled,
    _run_http,
    _stateless_http_enabled,
    _transport_security_settings,
)


def _prewarm_one(model_name: str) -> None:
    """Construct the embedding model and, for on-device models, force the
    lazy weight load with a real embed.

    A remote OpenAI-compatible backend has no weights to warm; constructing
    the client is enough, and we must NOT make a network round-trip on the
    prewarm path (R10: readiness must not depend on an upstream call).
    """
    model = _get_embed_model(model_name, Verbosity.QUIET)
    if model is not None and not is_remote_embed_model(model_name):
        list(model.embed("warmup"))


def _prewarm_embedding_model_async() -> None:
    """Kick off embedding-model pre-warming in a background daemon
    thread. Returns immediately so the MCP server can register and
    accept tool calls without blocking on a ~10s sentence-transformers
    load (Claude startup felt like a hang otherwise).

    If a search_corpus call arrives before the prewarm finishes, the
    lazy load in `_get_embed_model` runs synchronously on the search
    thread — same total latency, paid by the call that needs it.
    The `_MODEL_LOAD_LOCK` in models.py serialises the two paths so
    we don't load twice.
    """
    # Scan the index dir (defaults to the cache root) for a model to warm.
    root = get_index_dir()
    if not os.path.isdir(root):
        return
    model_name: Optional[str] = None
    for name in sorted(os.listdir(root)):
        db_path = os.path.join(root, name, "embeddings.db")
        if not os.path.isfile(db_path):
            continue
        # Busy timeout so this read waits out a concurrent gather write
        # instead of erroring with "database is locked". A sqlite3 connection
        # used as a context manager only scopes the transaction, not the
        # connection, so close it explicitly to avoid leaking one fd per
        # corpus scanned before a model is found.
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=30.0)
            row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
            if row:
                model_name = row[0]
                break
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
    if not model_name:
        return

    def _worker() -> None:
        try:
            _prewarm_one(model_name)
        except Exception:  # pylint: disable=broad-except
            # Best-effort: any failure here means lazy load on the
            # first search_corpus call takes over. Stay silent — we're
            # in the background; the search-path error log will fire
            # if loading is genuinely broken.
            pass

    threading.Thread(
        target=_worker,
        name="ietf-llm-prewarm",
        daemon=True,
    ).start()


def _load_server_instructions() -> str:
    """Return the server's built-in routing + norms guidance, served as the
    FastMCP `instructions` field (which compliant clients surface to the model
    as system-prompt context).

    Loaded from the bundled `data/mcp-instructions.md` — the routing brain
    (recognise the corpus, prefer it to web search, the mandatory read-the-norms
    gate before drafting or characterising consensus, the tool playbook). It is
    the server's own guidance, not a skill: nothing installs it, and every
    client — Claude, Codex, Gemini, Cursor, Zed, opencode — gets the same
    routing from this field. Frontmatter (if any) is stripped; a missing file
    degrades to an empty string so the server still comes up.
    """
    try:
        path = resources.files("ietf_llm").joinpath("data/mcp-instructions.md")
        text = _strip_frontmatter(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return ""
    # The mode-specific session facts must reach every client. Substitute at the
    # placeholder; if the bundled markdown ever drops it, prepend rather than let
    # the block silently vanish. The marker path is covered by
    # test_load_server_instructions_states_session_mode, the prepend fallback by
    # test_load_server_instructions_prepends_session_when_marker_missing.
    section = _session_section()
    if "{{SESSION}}" in text:
        return text.replace("{{SESSION}}", section)
    return f"{section}\n\n{text}"


# Named capability flags a skill or downstream tool can gate on, so it
# checks "does feature X exist" instead of comparing version numbers
# (brittle the moment a feature is backported or renamed). The version
# itself is the canonical protocol identity in `serverInfo.version`; this
# list is the agent-readable feature set, surfaced in `instructions` (the
# model never sees `serverInfo`). Add a flag here when you land a feature a
# skill might depend on; never remove one without a real capability change.
SERVER_FEATURES: tuple[str, ...] = (
    "live-lookup",  # overview(live=), draft_status, draft_authors, meeting_schedule
    "label-digest",  # read_digest(label=) — label-filtered issue/thread digests
)


def _capability_footer() -> str:
    """A single agent-readable line stating the running server's version and
    capability flags, appended to the MCP `instructions` field so a skill can
    confirm a feature is present (and tell the user to upgrade if not).

    The model never sees `serverInfo.version` — that reaches the host, not the
    prompt — so feature-gating lives here, in text the client surfaces as
    system context. Gate on the named flags, not on the version number."""
    features = ", ".join(SERVER_FEATURES)
    return (
        f"\n\n---\n\n_ietf-llm server version {__version__}; "
        f"features: {features}. A skill that needs a feature should check "
        "for its flag here and ask the user to upgrade ietf-llm if absent._"
    )


async def _run_with_threaded_writer(server: Any) -> None:
    """Wire FastMCP's lowlevel server up to our threaded-writer stdio
    transport. Mirrors `FastMCP.run_stdio_async` (the function our
    transport replaces) line-for-line, swapping the transport."""
    async with _stdio_transport.stdio_server_threaded_writer() as (
        read_stream,
        write_stream,
    ):
        # `_mcp_server` is the lowlevel `mcp.server.Server` instance
        # FastMCP wraps. Private attribute, but stable in practice —
        # the upstream `run_stdio_async` uses the same name.
        await server._mcp_server.run(  # pylint: disable=protected-access
            read_stream,
            write_stream,
            server._mcp_server.create_initialization_options(),  # pylint: disable=protected-access
        )


def _startup_gather_default() -> bool:
    """The in-session gather default resolved at startup, before the
    registration gate.

    On for a local stdio server (that user can already run `ietf-llm`
    against the same cache), off for the shared HTTP deployment (which stays
    read-only), and off regardless when `IETF_LLM_INDEX_IMMUTABLE` marks the
    mount read-only — a read-only mount must not *default* a writer on (the
    HTTP path already hard-refuses the explicit `ENABLE_GATHER` + immutable
    combination; this keeps the stdio default from silently picking a writer
    that would fail at index-write time). `IETF_LLM_ENABLE_GATHER` is still
    an explicit override on top of this default, in either direction.
    """
    return _resolve_transport() == "stdio" and not _index_immutable_enabled()


@graceful_keyboard_interrupt
def main() -> None:  # pylint: disable=too-many-locals
    try:
        from mcp.server.fastmcp import (  # pylint: disable=import-outside-toplevel,import-error
            FastMCP,
        )
    except ImportError:
        print(
            "The `mcp` package is missing — this should ship with "
            "ietf-llm. Try reinstalling: pipx install --force ietf-llm",
            file=sys.stderr,
        )
        sys.exit(1)

    # Diagnostic facility for investigating client-side stalls/timeouts.
    # Off by default; opt in per session by setting IETF_LLM_DEBUG_LOG=1
    # in the MCP server's launch env. When on, writes JSONL per-request
    # timing to a per-pid file under ~/.cache/ietf-llm/_debug/, and the
    # `get_session_log` tool returns its tail to the client.
    _debug_log.init()

    # Resolve the in-session gather default up front (see
    # `_startup_gather_default`: stdio on, http off, immutable mount off). It
    # must be established *before* the gather tools' registration gate below
    # so the gate and the user-facing "go gather" hints read the same resolved
    # value; IETF_LLM_ENABLE_GATHER still overrides either way.
    transport = _resolve_transport()
    set_gather_default(_startup_gather_default())
    # Record the transport so tool output can state the deployment topology
    # (local single-user vs possibly-shared) authoritatively — otherwise a
    # client infers it from transport-flavoured wording and hedges wrongly.
    set_deployment_mode(transport)

    # `instructions` is the MCP-spec mechanism for server-level guidance:
    # clients SHOULD surface it as system-prompt context. This carries the
    # full routing brain + the norms gate (from data/mcp-instructions.md) to
    # every client — Claude / Codex / Gemini / Cursor / Zed / opencode — so
    # routing needs no separately-installed skill. The version/feature footer
    # is appended so a client can feature-gate from the prompt.
    server_instructions = _load_server_instructions() + _capability_footer()
    # HTTP transport knobs (ignored by stdio): stateless sessions (default on,
    # so any replica answers any request) and an optional Host/Origin allow-list
    # for DNS-rebinding protection when the server is fronted directly (#41).
    server = FastMCP(
        "ietf-llm",
        instructions=server_instructions,
        stateless_http=_stateless_http_enabled(),
        transport_security=_transport_security_settings(),
    )
    # Report ietf-llm's own version in the `initialize` handshake's
    # `serverInfo.version` (what `claude mcp`, Cursor, etc. display). FastMCP's
    # constructor takes no `version`, so the lowlevel server otherwise falls
    # back to reporting the `mcp` SDK version. Same private-but-stable
    # `_mcp_server` attribute the stdio transport already drives below.
    server._mcp_server.version = __version__  # pylint: disable=protected-access

    # --- register the read-only, always-on tools ---
    corpus.register(server)
    rfcs.register(server)
    norms.register(server)
    citations.register(server)
    digest.register(server)
    search.register(server)
    topic.register(server)
    chunks.register(server)
    drafts.register(server)
    meetings.register(server)

    # `get_session_log` is only registered when telemetry is enabled
    # (IETF_LLM_DEBUG_LOG=1). With logging off the tool wouldn't have
    # anything useful to return, so we leave it out of the advertised
    # tool list entirely rather than ship a no-op tool.
    if _debug_log.is_enabled():
        gather.register_session_log(server)

    # `start_gather` / `gather_status` write to the cache and reach the
    # network — the one break from this server's read-only / no-network
    # contract — so they are registered only when gather is enabled. That
    # defaults on for local stdio and off for the shared HTTP replica;
    # IETF_LLM_ENABLE_GATHER overrides either way. `meeting_schedule` and
    # `draft_status` reach Datatracker live, so they ride the same gate.
    if _gather_enabled():
        gather.register(server)
        meetings.register_live(server)
        drafts.register_live(server)

    _prewarm_embedding_model_async()
    if transport == "http":
        # Shared-server deployment: standard MCP Streamable HTTP. The
        # threaded-writer transport below is stdio-specific.
        _run_http(server)
        return
    # Replace FastMCP.run() with our own stdio transport. The default
    # upstream transport writes outbound responses on the asyncio loop
    # via `await stdout.write(...)`, and a slow client backpressures
    # those writes through the kernel pipe buffer — stalling every
    # queued response invisibly. Our transport hands serialized bytes
    # to a daemon thread via a bounded in-process queue, so the loop
    # never awaits a kernel write. See ietf_llm/_stdio_transport.py.
    anyio.run(_run_with_threaded_writer, server)
