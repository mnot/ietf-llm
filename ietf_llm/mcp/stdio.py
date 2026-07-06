"""Stdio transport with a threaded writer.

Why this exists. The upstream stdio transport
(`mcp.server.stdio.stdio_server`) writes serialized responses to stdout
on the asyncio event loop:

    await stdout.write(json + "\\n")
    await stdout.flush()

Even though anyio dispatches each write to a worker thread, the
awaiting task is parked until the kernel write returns. Because MCP
framing forces all responses to serialize through one stdout, a full
pipe buffer (16-64 KB on macOS, kernel-fixed) backpressures every
queued response: the loop stays "alive" but every outbound message
sits behind a stuck write. A slow or wedged client therefore stalls
the server invisibly.

This transport decouples the loop from the kernel write. Outbound
`SessionMessage`s are serialized inline (cheap), enqueued onto a
bounded in-process queue, and drained to stdout by a dedicated daemon
thread. The loop never awaits a kernel write, so a slow client can
never stall the event loop — until the queue itself fills, which is
named, logged, and bounded.

The reader half is unchanged from upstream — backpressure on the
inbound side doesn't have the same single-serialization-point problem,
and the upstream reader is correct.

This is a drop-in replacement for `stdio_server`: same yield contract
(`read_stream`, `write_stream`), used the same way by the lowlevel
`Server.run`. We don't monkey-patch anything; we reach the lowlevel
server via `FastMCP._mcp_server` and pass our streams to it directly.

Observability: `queue_state()` returns a snapshot of queue depth,
high-water mark, bytes/items written, time the loop has spent blocked
on `put` (the bounded-queue backstop), and last write error. Hook it
into whatever telemetry is appropriate.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from contextlib import asynccontextmanager
from io import TextIOWrapper
from typing import Any, AsyncIterator, Optional, Tuple

import anyio
import anyio.lowlevel
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

DEFAULT_QUEUE_MAX_ITEMS = 1024
"""Cap on queued outbound messages. A typical response is a few KB
to a few tens of KB; 1024 items ≈ tens of MB worst case. Past this,
the async writer hands off to a worker thread for the blocking
`put` — at which point the symptom (slow/wedged client) is explicit
and timed rather than invisible."""

_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "queue_depth": 0,
    "queue_depth_max": 0,
    "bytes_written": 0,
    "items_written": 0,
    "writer_alive": False,
    "blocked_on_put_ms": 0.0,
    "last_write_error": None,
}


def queue_state() -> dict[str, Any]:
    """Snapshot of the writer queue's state. Safe to call from any
    thread / the asyncio loop."""
    with _state_lock:
        return dict(_state)


def _set(field: str, value: Any) -> None:
    with _state_lock:
        _state[field] = value
        if field == "queue_depth" and value > _state["queue_depth_max"]:
            _state["queue_depth_max"] = value


def _increment(field: str, delta: float) -> None:
    with _state_lock:
        _state[field] = _state[field] + delta


@asynccontextmanager
async def stdio_server_threaded_writer(
    max_queue_items: int = DEFAULT_QUEUE_MAX_ITEMS,
) -> AsyncIterator[
    Tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Drop-in replacement for `mcp.server.stdio.stdio_server`.

    Yields the same `(read_stream, write_stream)` pair upstream does,
    so it slots into `Server.run(read, write, init_options)` with no
    other changes."""
    stdin = anyio.wrap_file(
        TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    )
    # Outbound side writes raw bytes from the daemon thread; bypass
    # Python's text wrapper to avoid interleaving with anything else
    # that might still hold a reference to sys.stdout.
    stdout_fd = sys.stdout.fileno()

    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    write_queue: queue.Queue[Optional[bytes]] = queue.Queue(maxsize=max_queue_items)
    # Set when the writer thread has exited (normally or on a broken pipe). A
    # producer parked on a full queue watches this so it can never block
    # forever behind a dead consumer that will never drain again.
    writer_gone = threading.Event()

    def writer_thread() -> None:
        _set("writer_alive", True)
        try:
            while True:
                data = write_queue.get()
                if data is None:
                    break
                try:
                    view = memoryview(data)
                    while view:
                        # write(2) can return a short count under signal
                        # interruption; loop until fully drained.
                        written = os.write(stdout_fd, view)
                        view = view[written:]
                    _increment("bytes_written", len(data))
                    _increment("items_written", 1)
                    _set("queue_depth", write_queue.qsize())
                except OSError as exc:
                    _set("last_write_error", repr(exc))
                    # Broken pipe / closed stdout — nothing useful left
                    # for this thread to do. Process will exit on its
                    # own when the read side notices EOF.
                    return
        finally:
            _set("writer_alive", False)
            writer_gone.set()

    thread = threading.Thread(
        target=writer_thread, name="ietf-llm-stdio-writer", daemon=True
    )
    thread.start()

    async def stdin_reader() -> None:
        # Verbatim from upstream stdio_server — the read side has no
        # equivalent backpressure pathology.
        try:
            async with read_stream_writer:
                async for line in stdin:
                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:  # pylint: disable=broad-except
                        await read_stream_writer.send(exc)
                        continue
                    await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    def _put_or_give_up(data: bytes) -> None:
        # Block until the queue has room, waking periodically to check whether
        # the writer thread has died. If it has, drop the message rather than
        # park forever behind a consumer that will never drain again.
        while not writer_gone.is_set():
            try:
                write_queue.put(data, timeout=0.25)
                return
            except queue.Full:
                continue

    async def stdout_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_text = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    data = (json_text + "\n").encode("utf-8")
                    try:
                        write_queue.put_nowait(data)
                    except queue.Full:
                        # Bounded queue saturated — client genuinely not
                        # draining. Hand the blocking `put` to a worker
                        # thread so the loop stays free for other tasks
                        # (cancellations, notifications) while we wait.
                        # Time the wait so it shows up in telemetry as
                        # an explicit, named stall instead of an
                        # invisible one.
                        before = time.monotonic()
                        await anyio.to_thread.run_sync(_put_or_give_up, data)
                        _increment(
                            "blocked_on_put_ms",
                            (time.monotonic() - before) * 1000.0,
                        )
                    _set("queue_depth", write_queue.qsize())
        except anyio.ClosedResourceError:  # pragma: no cover
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        try:
            yield read_stream, write_stream
        finally:
            # Tell the daemon writer to drain and exit. Non-blocking;
            # if the queue is full the sentinel is dropped and the
            # thread exits when the process does (it's a daemon).
            try:
                write_queue.put_nowait(None)
            except queue.Full:
                pass
