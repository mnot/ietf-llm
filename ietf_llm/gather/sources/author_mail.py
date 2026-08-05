"""Sender-scoped mail: one person's messages, in their threads.

`sync_mailing_list` pulls a whole list within the `--months` window,
which is right for a WG corpus and wrong for a person corpus — following
someone across `last-call@` would drag in thousands of messages that
aren't theirs to say anything about how they review.

So this searches by sender instead. IMAP `SEARCH FROM` is server-side
and cheap, and the person's full address set comes from Datatracker, so
a list where they never posted costs one search that returns nothing.

**But their messages alone are not enough.** A review comment is mostly
*reactive* — unreadable without the message it answers, and worthless
as evidence of judgement if you can't see what provoked it. Keeping only
their messages would produce a corpus of decontextualised fragments. So
each of their messages is expanded to the thread around it: take the
base subject, search the list for it, and download the whole
conversation. `mail_threads` then reconstructs threads from the `.eml`
cache exactly as it does for a WG corpus — this module writes into the
same `imap-cache/<wg>/<list>/` directory and nothing downstream needs
to know the messages arrived by a different route.

Two cases can't be hydrated, and both keep the person's own message
while losing the surrounding thread; each is counted and logged rather
than passed over silently:

  - **A non-ASCII subject.** IMAP `SEARCH SUBJECT` needs a `CHARSET`
    negotiation the anonymous archive server doesn't reliably support.
  - **A very short subject.** `SUBJECT` matches substrings, so hydrating
    "Agenda" would pull in every agenda mail the list ever carried.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import os
import re
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

#: Below this many characters a base subject is too generic to hydrate:
#: IMAP SUBJECT is a substring match, so "Agenda" would match the whole
#: list. Their own message is still kept.
MIN_SUBJECT_CHARS = 12

#: Ceiling on messages pulled in for one subject. A long-running thread
#: is legitimately large; a subject that matches thousands is a sign the
#: base subject was too generic to be a thread key.
MAX_UIDS_PER_SUBJECT = 300

#: Leading `Re:` / `Fwd:` / `[list-tag]` noise, stripped repeatedly to
#: get at the base subject two messages in a thread share.
_SUBJECT_NOISE = re.compile(
    r"^\s*(?:(?:re|aw|fwd?|fw)\s*:\s*|\[[^\]]{1,40}\]\s*)+", re.I
)
_WS_RUN = re.compile(r"\s+")


def base_subject(raw: str) -> str:
    """The thread-shared part of a subject line.

    Strips any run of `Re:` / `Fwd:` prefixes and list tags, then
    collapses whitespace (archived mail is often re-wrapped, and the
    IMAP search has to match the stored form).
    """
    return _WS_RUN.sub(" ", _SUBJECT_NOISE.sub("", raw or "")).strip()


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
    non-OK status — a search that the server rejects (an odd subject,
    say) must not abort the whole list."""
    try:
        status, data = mail.uid("search", criteria)
    except (imaplib.IMAP4.error, OSError):
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return [uid for uid in data[0].split() if isinstance(uid, bytes)]


def _subjects_for_uids(mail: imaplib.IMAP4_SSL, uids: Sequence[bytes]) -> List[str]:
    """Fetch just the Subject header for each UID."""
    out: List[str] = []
    for start in range(0, len(uids), 200):
        batch = ",".join(u.decode() for u in uids[start : start + 200])
        try:
            status, data = mail.uid(
                "fetch", batch, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])"
            )
        except (imaplib.IMAP4.error, OSError):
            continue
        if status != "OK" or not data:
            continue
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            body = item[1]
            if not isinstance(body, bytes):
                continue
            message = email.message_from_bytes(body, policy=email.policy.default)
            subject = str(message.get("Subject") or "")
            if subject:
                out.append(subject)
    return out


class _Hydration:
    """Counters for what the thread expansion could and couldn't do."""

    def __init__(self) -> None:
        self.non_ascii = 0
        self.too_short = 0
        self.capped = 0

    def note(self, verbose: Verbosity, list_name: str) -> None:
        if self.non_ascii or self.too_short:
            log(
                f"  '{list_name}': {self.non_ascii} non-ASCII and "
                f"{self.too_short} too-generic subject(s) kept without "
                "surrounding thread context.",
                verbose,
                level=LogLevel.PROGRESS,
            )
        if self.capped:
            log(
                f"  '{list_name}': {self.capped} subject(s) matched more than "
                f"{MAX_UIDS_PER_SUBJECT} messages; truncated to that.",
                verbose,
                level=LogLevel.WARN,
            )


def _thread_uids(
    mail: imaplib.IMAP4_SSL,
    subjects: Sequence[str],
    since: str,
    stats: _Hydration,
) -> Set[bytes]:
    """UIDs of every message sharing a base subject with one of theirs."""
    found: Set[bytes] = set()
    seen: Set[str] = set()
    for raw in subjects:
        base = base_subject(raw)
        if not base or base in seen:
            continue
        seen.add(base)
        if not base.isascii():
            stats.non_ascii += 1
            continue
        if len(base) < MIN_SUBJECT_CHARS:
            stats.too_short += 1
            continue
        uids = _search_uids(mail, f"({since}SUBJECT {_quote(base)})")
        if len(uids) > MAX_UIDS_PER_SUBJECT:
            stats.capped += 1
            uids = uids[-MAX_UIDS_PER_SUBJECT:]
        found.update(uids)
    return found


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

        stats = _Hydration()
        subjects = _subjects_for_uids(mail, sorted(theirs))
        wanted = theirs | _thread_uids(mail, subjects, since, stats)
        stats.note(verbose, list_name)

        cached = {n for n in os.listdir(cache_dir) if n.endswith(".eml")}
        missing = [u for u in sorted(wanted) if f"{u.decode()}.eml" not in cached]
        new_count = 0
        if missing:
            new_count = _download_batches(
                mail, missing, cache_dir, verbose, on_progress
            )
        log(
            f"  '{list_name}': {len(theirs)} message(s) from this person, "
            f"{len(wanted)} with thread context ({new_count} new).",
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

    Messages land in the same per-list `.eml` cache a whole-list sync
    uses, so `mail_threads.write_thread_files` reconstructs them with no
    special handling. Returns the total newly downloaded.

    A list that can't be selected or that errors is reported and skipped
    — with a dozen speculative lists in play, one bad folder must not
    take the gather down.
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
