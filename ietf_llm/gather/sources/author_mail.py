"""Sender-scoped mail: one person's messages, quotes intact.

`sync_mailing_list` pulls a whole list within the `--months` window,
which is right for a WG corpus and wrong for a person corpus — following
someone across `last-call@` would bury them in thousands of messages
that aren't theirs.

So this searches by sender instead. IMAP `SEARCH FROM` is server-side
and cheap, and the person's full address set comes from Datatracker, so
a list where they never posted costs one search that returns nothing.

**The context comes from the quotes, not from the thread.** A reply is
unreadable without what it answers — but the thing it answers is already
sitting in the message, quoted, and quoted *selectively*: the sender
trimmed it down to the part they were actually responding to. That is
better-targeted context than the surrounding thread, which carries every
sub-branch they ignored. So there is no thread hydration here; instead
`mail_threads` keeps the quote trail intact for any message whose parent
isn't in the same file, which for an author corpus is nearly all of
them.

What that trades away is the *reaction* — whether anyone pushed back,
and whether they conceded. A corpus built this way is evidence about
what this person raises, not about how it landed.

Messages are written into the same `imap-cache/<wg>/<list>/` directory a
whole-list sync uses, so `mail_threads` reconstructs them with no
knowledge that they arrived by a different route.
"""

from __future__ import annotations

import imaplib
import os
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence, Set

from ...log import LogLevel, Verbosity, log
from ...paths import get_cache_dir
from .mbox import (  # pylint: disable=protected-access
    IMAP_PASS,
    IMAP_PORT,
    IMAP_SERVER,
    IMAP_TIMEOUT,
    IMAP_USER,
    _download_batches,
    _FolderSelectError,
    normalize_list_name,
)


def _quote(value: str) -> str:
    """Quote a string for an IMAP search term."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _since_term(months: Optional[int]) -> str:
    if not months:
        return ""
    since = (datetime.now() - timedelta(days=30 * months)).strftime("%d-%b-%Y")
    return f'SINCE "{since}" '


def _search_uids(mail: imaplib.IMAP4_SSL, criteria: str) -> List[bytes]:
    """Run one UID SEARCH, returning [] rather than raising on a
    non-OK status — one address the server chokes on must not abort
    the whole list."""
    try:
        status, data = mail.uid("search", criteria)
    except (imaplib.IMAP4.error, OSError):
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return [uid for uid in data[0].split() if isinstance(uid, bytes)]


def _sync_one_list(
    wg_name: str,
    list_name: str,
    addresses: Sequence[str],
    months: Optional[int],
    verbose: Verbosity,
    on_progress: Optional[Callable[[int, int], None]],
) -> int:
    """Sender-scoped sync of one list. Returns messages newly downloaded."""
    cache_dir = os.path.join(get_cache_dir(), "imap-cache", wg_name, list_name)
    os.makedirs(cache_dir, exist_ok=True)
    since = _since_term(months)

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_TIMEOUT)
    try:
        mail.login(IMAP_USER, IMAP_PASS)
        status, _ = mail.select(f'"Shared Folders/{list_name}"', readonly=True)
        if status != "OK":
            raise _FolderSelectError(list_name)

        theirs: Set[bytes] = set()
        for address in addresses:
            theirs.update(_search_uids(mail, f"({since}FROM {_quote(address)})"))
        if not theirs:
            log(
                f"  '{list_name}': no messages from this person in the window.",
                verbose,
                level=LogLevel.PROGRESS,
            )
            return 0

        cached = {n for n in os.listdir(cache_dir) if n.endswith(".eml")}
        missing = [u for u in sorted(theirs) if f"{u.decode()}.eml" not in cached]
        new_count = 0
        if missing:
            new_count = _download_batches(
                mail, missing, cache_dir, verbose, on_progress
            )
        log(
            f"  '{list_name}': {len(theirs)} message(s) from this person "
            f"({new_count} new).",
            verbose,
            level=LogLevel.STATUS,
        )
        return new_count
    finally:
        try:
            mail.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def sync_author_mail(
    wg_name: str,
    list_names: Sequence[str],
    addresses: Sequence[str],
    months: Optional[int] = None,
    verbose: Verbosity = Verbosity.STATUS,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    note_fn: Optional[Callable[[str], None]] = None,
) -> int:
    """Sync every list in `list_names`, scoped to `addresses` as sender.

    Returns the total newly downloaded. A list that can't be selected or
    that errors is reported and skipped — with a dozen speculative lists
    in play, one bad folder must not take the gather down.
    """
    if not list_names or not addresses:
        return 0
    log(
        f"Searching {len(list_names)} list(s) for mail from "
        f"{len(addresses)} address(es)...",
        verbose,
        level=LogLevel.STATUS,
    )
    total = 0
    for raw_name in list_names:
        list_name = normalize_list_name(raw_name)
        if not list_name:
            continue
        per_list_cb: Optional[Callable[[int, int], None]] = None
        if on_progress is not None:

            def per_list_cb(  # pylint: disable=function-redefined
                done: int, count: int, _name: str = list_name
            ) -> None:
                on_progress(_name, done, count)

        try:
            total += _sync_one_list(
                wg_name, list_name, addresses, months, verbose, per_list_cb
            )
        except _FolderSelectError:
            message = (
                f"Mailing list '{list_name}': no such folder on the IETF IMAP "
                "server; skipped."
            )
            log(message, verbose, level=LogLevel.WARN)
            if note_fn is not None:
                note_fn(message)
        except (imaplib.IMAP4.error, OSError) as err:
            message = f"Mailing list '{list_name}': IMAP error ({err}); skipped."
            log(message, verbose, level=LogLevel.WARN)
            if note_fn is not None:
                note_fn(message)
    return total
