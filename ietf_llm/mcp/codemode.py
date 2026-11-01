"""Code mode: run model-written Python over the gathered corpora.

The code runs in a Monty sandbox (pydantic-monty: a Rust Python interpreter in
a pool of worker subprocesses). Its only way out is the `ietf` module below, a
handful of read-only functions over the local index, so the read tools'
offline / read-only boundary holds by construction: no filesystem, network or
environment access from the sandbox.

One `execute` call can loop, filter and join across files and corpora and
return just the answer, where the classic tools would need a round trip (and a
page of rendered markdown in the caller's context) per step.
"""

from __future__ import annotations

import atexit
import contextvars
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic_monty import (
    CollectString,
    ExternalModule,
    Monty,
    MontyError,
    ResourceLimits,
)

from ..embeddings import search as _search
from ..embeddings import count_chunks, probe_index, select_chunks
from ..gather.sources.mail_threads import THREAD_MSG_HEADER_RE
from .common import (
    _biggest_grounding_thread,
    _grounding_frame,
    _index_rebuilding_note,
    _list_wgs,
    _offload_scan,
    _participation_nudge,
    _regather_call,
    _requires_corpus,
)

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer  # pragma: no cover

#: Execution time only (the clock stops while a host call runs), plus a cap on
#: host round trips so a runaway loop over `chunks()` ends.
_LIMITS: ResourceLimits = {
    "max_feed_duration_secs": 20.0,
    "max_suspensions": 2000,
    "max_memory": 512 * 1024 * 1024,
}
_MAX_OUTPUT_CHARS = 60_000
_MAX_LIMIT = 5000
_T = TypeVar("_T")

STUBS = '''\
from typing import TypedDict

class Chunk(TypedDict):
    file: str            # e.g. "threads/2026-10-22-new-i-d-….md", "issues/<owner>-<repo>/12.md"
    idx: int             # message number in the file (chunk_idx elsewhere)
    date: str | None     # ISO 8601 UTC; None for drafts / slides
    title: str           # header line: "[3] 2026-10-27 17:11 — Name (Role) (reply to [2])"
    sender: str | None   # "Name (Role)" parsed from the header, None if not a message
    reply_to: int | None # idx of the parent message in the same file
    url: str | None      # mail archive / GitHub permalink
    state: str | None    # "open" / "closed" for issue chunks
    labels: str | None   # comma-separated GitHub labels
    text: str            # whole message body (see `chars`)

class Hit(TypedDict):
    score: float
    file: str
    chunk_idx: int
    title: str
    snippet: str
    url: str | None

def corpora() -> list[str]:
    """Names of the gathered corpora (WGs, lists, draft sets)."""

def chunks(
    corpus: str, *, file: str | None = None, kind: str | None = None,
    since: str | None = None, until: str | None = None, author: str | None = None,
    contains: str | None = None, chars: int | None = None, limit: int = 200,
) -> list[Chunk]:
    """Messages / issue comments / draft windows, oldest first. Filters run in
    SQL: `kind` is the top directory ("threads", "issues", "drafts",
    "meetings"; pull requests are not indexed, use the get_issue tool);
    `since`/`until` are ISO dates (inclusive); `author` and `contains` are
    case-insensitive substrings of the header / body. `chars`
    truncates bodies (0 drops them: use it when you only count or group).
    `limit` max 5000; a full list may be cut off, so count with count()."""

def count(
    corpus: str, *, file: str | None = None, kind: str | None = None,
    since: str | None = None, until: str | None = None, author: str | None = None,
    contains: str | None = None,
) -> int:
    """How many chunks match: same filters as chunks(), no limit. Use it for
    totals; len(chunks(...)) stops at `limit`."""

def search(
    corpus: str, query: str, k: int = 10, *, since: str | None = None,
    until: str | None = None, author: str | None = None,
) -> list[Hit]:
    """Semantic search, best first."""
'''


@dataclass
class _Run:
    """What one `execute` run read: the files per corpus (for the grounding
    frame and participation nudge the classic read tools add) and how many
    `chunks()` calls came back full, so possibly cut off at their limit."""

    files: dict[str, set[str]] = field(default_factory=dict)
    capped: int = 0


_run: contextvars.ContextVar[_Run] = contextvars.ContextVar("codemode_run")


def _guarded(corpus: str, read: Callable[[], _T]) -> _T:
    """Run `read()` behind the read tools' corpus gate (name check, first-gather
    refusal, version pin, access stamp). A refusal, or an empty result that
    comes from a missing or rebuilding index rather than from the data,
    becomes the sandbox error, so it is never read as an absence."""

    def _read(_wg: str) -> _T:
        result = read()
        if not result:
            note = _index_rebuilding_note(corpus)
            if note is not None:
                raise RuntimeError(f"no results yet: {note}")
            if not probe_index(corpus):
                raise RuntimeError(
                    f"corpus {corpus!r} has no readable index, so this empty "
                    f"result is not an absence. Re-gather with {_regather_call(corpus)}"
                )
        return result

    result = _requires_corpus(_read)(corpus)
    if isinstance(result, str):
        raise ValueError(result)
    run = _run.get(None)
    if run is not None and isinstance(result, list):
        files = run.files.setdefault(corpus, set())
        files.update(row["file"] for row in cast(list[dict[str, Any]], result))
    return result


def _sender(chunk: dict[str, Any]) -> dict[str, Any]:
    match = THREAD_MSG_HEADER_RE.match("### " + chunk["title"])
    chunk["sender"] = match["sender"] if match else None
    chunk["reply_to"] = int(match["reply_to"]) if match and match["reply_to"] else None
    return chunk


def chunks(  # noqa: PLR0913
    corpus: str,
    *,
    file: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    contains: str | None = None,
    chars: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    limit = min(limit, _MAX_LIMIT)
    rows = _guarded(
        corpus,
        lambda: select_chunks(
            corpus,
            file=file,
            kind=kind,
            since=since,
            until=until,
            author=author,
            contains=contains,
            chars=chars,
            limit=limit,
        ),
    )
    run = _run.get(None)
    if run is not None and limit and len(rows) >= limit:
        run.capped += 1
    return [_sender(row) for row in rows]


def count(  # noqa: PLR0913
    corpus: str,
    *,
    file: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    contains: str | None = None,
) -> int:
    return _guarded(
        corpus,
        lambda: count_chunks(
            corpus,
            file=file,
            kind=kind,
            since=since,
            until=until,
            author=author,
            contains=contains,
        ),
    )


def search(  # noqa: PLR0913
    corpus: str,
    query: str,
    k: int = 10,
    *,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
) -> list[dict[str, Any]]:
    hits = _guarded(
        corpus,
        lambda: [
            {key: value for key, value in asdict(hit).items() if value is not None}
            for hit in _search(
                corpus, query, k=min(k, 50), since=since, until=until, author=author
            )
        ],
    )
    # Over a present index, an unfiltered top-k always has hits; none means the
    # query was not embedded (model unavailable or the call failed).
    if not hits and k > 0 and not (since or until or author):
        raise RuntimeError(
            "search() could not embed the query, so this is not an absence. "
            "Use chunks(contains=...) for exact text instead."
        )
    return hits


_MODULE = ExternalModule(
    {"corpora": _list_wgs, "chunks": chunks, "count": count, "search": search},
    stubs=STUBS,
)

_pool: Monty | None = None
_pool_lock = threading.Lock()


def _monty() -> Monty:
    """The process-wide worker pool, started on first use."""
    global _pool  # noqa: PLW0603
    with _pool_lock:
        if _pool is None:
            pool = Monty()
            pool.__enter__()  # noqa: PLC2801 -- lives for the process, not a `with`
            atexit.register(pool.__exit__, None, None, None)
            _pool = pool
        return _pool


def tool_execute(code: str) -> str:
    """Run `code` in a fresh sandbox session; return what it printed plus the
    value of its last expression (JSON), or the sandbox's error."""
    printed = CollectString(max_bytes=_MAX_OUTPUT_CHARS)
    run = _Run()
    _run.set(run)
    try:
        with _monty().checkout(
            external_modules={"ietf": _MODULE}, limits=_LIMITS
        ) as session:
            result = session.feed_run(code, print_callback=printed)
    except MontyError as exc:
        display = getattr(exc, "display", None)
        return f"Error:\n{display() if display else exc}"
    out = printed.output
    if result is not None:
        out += json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), default=str
        )
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + "\n… [truncated: return less, or aggregate]"
    out = out or "(no output: end with an expression or print())"
    # Same guards as the read tools: frame on top, nudge as a footer.
    if run.capped:
        out += (
            f"\n\n> chunks() returned its full `limit` in {run.capped} call(s), "
            "so those lists may be cut off and len() of them is not a total. "
            "Use count() for totals, or narrow with since/until/file."
        )
    # One frame, for the biggest qualifying thread across every corpus read.
    biggest = max(
        (
            (best[0], wg, best[2])
            for wg, files in run.files.items()
            if (best := _biggest_grounding_thread(wg, sorted(files)))
        ),
        default=None,
    )
    frame = _grounding_frame(biggest[1], [biggest[2]]) if biggest else ""
    nudge = _participation_nudge([f for fs in run.files.values() for f in fs])
    return (
        frame + ("\n" if frame else "") + out + (f"\n\n---\n\n{nudge}" if nudge else "")
    )


_DESCRIPTION = """Run Python over the gathered corpora and return only the answer.

Use for anything that loops, filters, counts or joins: one person's
posts across several corpora, every thread on a draft since a date,
who replied to whom, messages mentioning a string. One call replaces
many `search_corpus` / `get_chunk_text` / `read_digest` round trips,
and only the final value comes back.

The code runs in a sandbox (a subset of Python: no third-party
packages, no files, no network). Import the API with
`from ietf import corpora, chunks, count, search`. The value of the last
expression is returned as JSON, after anything printed:

```python
from ietf import corpora, count
posts = {c: count(c, author="Mark Nottingham", since="2026-08-01",
                  kind="threads")
         for c in corpora()}
{c: n for c, n in posts.items() if n}
```

Counting is not consensus: consensus is declared by the chairs, so a
tally of +1s is signal, not an outcome (read_ietf_interpretation_norms).

API (`ietf` module):

""" + STUBS


# Appended to the MCP instructions only when code mode is on, so the routing
# brain never names a tool the server did not register.
INSTRUCTIONS = """

**Code mode:** `execute` runs Python over the corpora and returns only the
result. Reach for it when an answer needs a loop, a count or a join (one
person across corpora, every message on a draft since a date, reading many
messages at once) instead of chaining many read calls. In this session
`read_topic`, `search_corpora`, `find_replies` and `get_chunks_batch` are not
available, wherever the text above names them: do that work in `execute`."""


# Read tools `execute` covers in a few lines; unregistered when code mode is on.
REPLACES = ("read_topic", "search_corpora", "find_replies", "get_chunks_batch")


def register(server: MCPServer) -> None:
    @server.tool(description=_DESCRIPTION)
    async def execute(code: str) -> str:
        return await _offload_scan(tool_execute, code)
