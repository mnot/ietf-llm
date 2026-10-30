"""Fetching IMAP messages into the per-list cache, and finding which UIDs a
server will not deliver when a batch comes back short."""

from __future__ import annotations

import imaplib
import os
import re
from typing import List, Optional

from ...atomicio import atomic_open_binary
from ...log import LogLevel, Verbosity, log


class ShortFetch(imaplib.IMAP4.error):
    """Some requested messages were not written to the cache. A subclass of
    `IMAP4.error` so the caller's retry covers it (a retry only refetches what
    is still missing); `uids` is filled in by `_imap_sync_attempt`, which is
    what knows the window. `undeliverable` lists the UIDs a fetch of that UID
    alone was refused for; the rest of the shortfall was not isolated."""

    def __init__(
        self, missing: int, requested: int, undeliverable: Optional[List[str]] = None
    ) -> None:
        super().__init__(f"{missing} of {requested} requested message(s) not fetched")
        self.missing = missing
        self.requested = requested
        self.written = requested - missing
        self.undeliverable = undeliverable or []
        self.uids: List[str] = []


#: Bounds on isolating undeliverable UIDs in a refused batch: halving a batch of
#: BATCH_SIZE reaches a single UID in 8 levels, and the request cap keeps a
#: server that refuses everything from turning one list into hundreds of
#: round trips.
BISECT_MAX_DEPTH = 8
BISECT_MAX_REQUESTS = 40


def fetch_into_cache(
    mail: imaplib.IMAP4_SSL,
    uids: List[bytes],
    cache_dir: str,
    verbose: Verbosity,
) -> set[str]:
    """FETCH `uids` in one request and write what comes back; returns the UIDs
    written. A refused request or a reply that omits some UIDs just writes less."""
    wanted = {uid.decode() for uid in uids}
    written: set[str] = set()
    status, msg_data = mail.uid("fetch", ",".join(sorted(wanted, key=int)), "(RFC822)")
    if status != "OK" or not msg_data:
        reply = msg_data[0] if msg_data else b""
        text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else ""
        detail = f": {text}" if text else ""
        log(
            f"  FETCH of {len(uids)} message(s) returned {status!r}{detail}.",
            verbose,
            level=LogLevel.WARN,
        )
        return written

    for item in msg_data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue

        # item[0] is the response header, item[1] is the message body
        header = item[0]
        if not isinstance(header, bytes):
            continue

        # Find UID in the response header
        uid_match = re.search(r"UID\s+(\d+)", header.decode())
        if not uid_match:
            continue

        msg_uid = uid_match.group(1)
        body = item[1]
        if msg_uid not in wanted or not isinstance(body, bytes):
            continue

        with atomic_open_binary(os.path.join(cache_dir, f"{msg_uid}.eml")) as handle:
            handle.write(body)
        written.add(msg_uid)
    return written


def isolate_undeliverable(
    mail: imaplib.IMAP4_SSL,
    failed: List[bytes],
    cache_dir: str,
    verbose: Verbosity,
    budget: List[int],
) -> tuple[set[str], List[str]]:
    """Fetch what can be fetched from `failed` (UIDs a bigger request did not
    deliver) by halving it, so one message the server won't deliver costs only
    itself rather than its whole batch. A group of one is fetched alone; if that
    is refused too, its UID is undeliverable. Works a level at a time, so that
    when `budget` (requests left, shared across the list) or
    `BISECT_MAX_DEPTH` runs out every part of the batch has been narrowed
    equally. Returns `(written, undeliverable)`; UIDs in neither were not
    reached."""
    written: set[str] = set()
    undeliverable: List[str] = []
    level = [failed]
    for _ in range(BISECT_MAX_DEPTH):
        nxt: List[List[bytes]] = []
        for group in level:
            halves = (
                [group]
                if len(group) == 1
                else [
                    group[: len(group) // 2],
                    group[len(group) // 2 :],
                ]
            )
            for part in halves:
                if budget[0] <= 0:
                    return written, undeliverable
                budget[0] -= 1
                got = fetch_into_cache(mail, part, cache_dir, verbose)
                written |= got
                left = [uid for uid in part if uid.decode() not in got]
                if len(part) == 1 and left:
                    undeliverable.append(left[0].decode())
                elif left:
                    nxt.append(left)
        level = nxt
        if not level:
            break
    return written, undeliverable
