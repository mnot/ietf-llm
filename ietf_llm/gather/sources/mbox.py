import email
import email.policy
import email.utils
import glob
import html
import imaplib
import os
import re
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage, MIMEPart
from typing import Callable, Dict, List, NamedTuple, Optional

import certifi
import requests

from ...atomicio import write_if_changed
from ...datatracker_api import get_mailing_list_name
from ...log import LogLevel, Verbosity, log
from ...net import DEFAULT_HEADERS, governed_get
from ...paths import get_cache_dir, raw_dir, raw_mail_archive_path
from ...tls import certificate_error, certificate_hint, system_trust_context
from .imap_fetch import (
    BISECT_MAX_REQUESTS,
    ShortFetch,
    fetch_into_cache,
    isolate_undeliverable,
)

IMAP_SERVER = "imap.ietf.org"
IMAP_PORT = 993
IMAP_USER = "anonymous"
IMAP_PASS = "mnot+ietf-llm@ietf.org"
# Messages per IMAP FETCH. Each batch is one command the server answers with a
# single (multi-message) response, so a larger batch is both faster (fewer
# round-trips on a first sync of a busy list) and gentler on the server (fewer
# commands). Messages are small, so 200 per response stays modest in memory.
BATCH_SIZE = 200
# Socket timeout (seconds) so a stalled server can't hang a gather
# indefinitely. Applies per blocking read, so it bounds stalls without
# capping the total transfer time of a large (chunked) response.
IMAP_TIMEOUT = 60
# Extra attempts after the first for a connection-level IMAP failure (dropped
# TLS, timeout, transient server error). One retry covers a momentary hiccup
# without turning a genuinely-down server into a long stall. A folder that
# won't select is *not* retried — that's a wrong list name, not a transient.
IMAP_RETRIES = 1
# Per-list file recording the IMAP UIDVALIDITY the cached `<uid>.eml` files were
# fetched under. UIDs only name a message within one UIDVALIDITY epoch.
UIDVALIDITY_FILE = ".uidvalidity"
_LIST_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
# Where a list's cached messages go when the server renumbers the folder:
# `<list>/epoch-<old UIDVALIDITY>/`. Their UIDs no longer name anything on the
# server, but the messages are still real mail (and the only copy of anything
# older than the search window), and thread reconstruction walks the list dir
# recursively, so they keep their threads.
EPOCH_DIR_PREFIX = "epoch-"


def validate_list_names(
    names: List[str], verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Return the subset of `names` that resolve on mailarchive.ietf.org.

    Used by the CLI to drop typo'd `--mailing-list` values BEFORE
    `config.merge` persists them. The probe is a GET against the
    list's browse page (`https://mailarchive.ietf.org/arch/browse/
    <list>/`), which returns 200 for known lists and 404 otherwise.
    Names normalised (`foo@ietf.org` → `foo`) for the probe; the
    returned list keeps the user's original form so the persisted
    value matches what they typed.
    """
    valid: List[str] = []
    for raw in names:
        norm = normalize_list_name(raw)
        if not norm:
            log(
                f"--mailing-list {raw!r}: not a mailing list name; not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        url = f"https://mailarchive.ietf.org/arch/browse/{norm}/"
        try:
            status: Optional[int] = governed_get(
                url, headers=DEFAULT_HEADERS, timeout=30
            ).status_code
        except requests.RequestException:
            status = None
        if status == 404:
            log(
                f"--mailing-list {raw}: not found on "
                "mailarchive.ietf.org; not persisting.",
                verbose,
                level=LogLevel.STATUS,
            )
            continue
        if status != 200:
            # A transient failure (network / 5xx) must not drop a name the user
            # explicitly asked for — only a definitive 404 does. Keep it and let
            # the gather surface any real problem later.
            log(
                f"--mailing-list {raw}: could not verify "
                f"(status {status}); keeping it anyway.",
                verbose,
                level=LogLevel.STATUS,
            )
        valid.append(raw)
    return valid


def normalize_list_name(raw: str) -> str:
    """Return just the list-name portion of an IETF mailing list address.

    `foo@ietf.org`  → `foo`
    `foo@irtf.org`  → `foo`  (IRTF RGs use the same IMAP server)
    `foo`           → `foo`  (already bare)

    Whitespace stripped; case lowered to match IMAP folder convention.
    Used by both `--mailing-list` argument parsing and the sync entry
    point so callers can pass either form. A name that is not a plain
    list name (it becomes a directory and an IMAP folder) comes back
    empty, which callers already treat as "no list".
    """
    cleaned = raw.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    return cleaned if _LIST_NAME_RE.fullmatch(cleaned) else ""


def extract_text_content(msg: EmailMessage) -> str:
    """Extract plain text from an EmailMessage, ignoring attachments and HTML."""
    try:
        body_part = msg.get_body(preferencelist=("plain",))
        if body_part:
            return _decode_safely(body_part)
    except (AttributeError, ValueError, TypeError, LookupError):
        pass

    # Fallback to manual walk for edge cases
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and part.get_filename() is None:
            if isinstance(part, EmailMessage):
                body += _decode_safely(part)
    return body


def _decode_safely(part: MIMEPart) -> str:
    """Attempt to decode plain text from an EmailMessage part safely."""
    try:
        # High-level API
        content = part.get_content()
        return str(content) if content is not None else ""
    except (AttributeError, ValueError, TypeError, LookupError):
        # Fallback: get raw bytes and decode manually with common fallbacks
        try:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                return ""
            # Try some common charsets with 'replace' error handling
            for charset in ["utf-8", "latin-1", "ascii"]:
                try:
                    return payload.decode(charset, errors="replace")
                except (ValueError, LookupError):
                    continue
            return payload.decode("ascii", errors="replace")
        except (AttributeError, ValueError, TypeError, LookupError):
            return ""


def clean_email_text(text: str) -> str:
    """Strip signatures and quoted replies from the text, and decode HTML entities."""
    # Decode HTML entities like &nbsp;
    text = html.unescape(text)

    lines = text.splitlines()
    cleaned_lines = []

    # Common signature starts (case-insensitive)
    sig_patterns = [
        re.compile(r"^(Best\s+|Kind\s+|Warm\s+)?Regards,?.*$", re.I),
        re.compile(
            r"^Sent\s+from\s+my\s+.*(iPhone|iPad|iPod|BlackBerry|Android|mobile|"
            r"mobile\s+device).*$",
            re.I,
        ),
        re.compile(r"^--\s*$"),
        re.compile(r"^-{3,}.*$"),
        re.compile(r"^_{3,}.*$"),
        re.compile(r"^=+\s*$"),
    ]

    for line in lines:
        stripped_line = line.strip()

        # Check for standard and common signature separators
        match_found = False
        for pattern in sig_patterns:
            if pattern.match(stripped_line):
                # Special case for 'Regards': only strip if it's a short line
                # (to avoid false positives with "Regards to...")
                if "regards" in stripped_line.lower():
                    if len(stripped_line) < 40:
                        match_found = True
                else:
                    match_found = True
                break

        if match_found:
            break

        if line.lstrip().startswith(">"):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def _download_batches(
    mail: imaplib.IMAP4_SSL,
    missing_uids: List[bytes],
    cache_dir: str,
    verbose: Verbosity,
    on_progress: Optional[Callable[[int, int], None]] = None,
    isolate: bool = False,
) -> int:
    """Download messages in batches and save to cache. Returns count of new
    messages. `on_progress`, when given, is called after each batch with
    `(downloaded_so_far, total_to_download)` for live progress reporting.

    Every batch is attempted, then `ShortFetch` is raised if any requested UID
    was not written: a non-OK reply, or a reply that left some UIDs out, would
    otherwise read as a complete sync and the mail would be absent from this
    run's threads. With `isolate` (the final attempt), a batch that came up
    short is halved to find which UIDs the server won't deliver."""
    written: set[str] = set()
    undeliverable: List[str] = []
    budget = [BISECT_MAX_REQUESTS]
    log(
        f"Downloading {len(missing_uids)} new messages in batches of {BATCH_SIZE}...",
        verbose,
        level=LogLevel.PROGRESS,
    )
    for i in range(0, len(missing_uids), BATCH_SIZE):
        batch = missing_uids[i : i + BATCH_SIZE]
        got = fetch_into_cache(mail, batch, cache_dir, verbose)
        written |= got
        left = [uid for uid in batch if uid.decode() not in got]
        if left and isolate:
            more, bad = isolate_undeliverable(mail, left, cache_dir, verbose, budget)
            written |= more
            undeliverable.extend(bad)

        if written:
            log(
                f"Downloaded {len(written)}/{len(missing_uids)} new messages...",
                verbose,
                level=LogLevel.PROGRESS,
            )
        if on_progress is not None:
            on_progress(len(written), len(missing_uids))
    if len(written) < len(missing_uids):
        raise ShortFetch(
            len(missing_uids) - len(written), len(missing_uids), undeliverable
        )
    return len(written)


#: Per-list sidecar recording the UIDVALIDITY the server reported *this gather*.
#: Lets the cloud persist tell "this gather confirmed the local cache's epoch
#: with the server" from "the cache is whatever an earlier run left" -- only the
#: former may replace stored mail. A file rather than process memory because the
#: sync runs in the gather child and the persist in the server that spawned it.
#: Scoped to one gather: `forget_verified` deletes a corpus's sidecars when its
#: gather starts, and each list's sync deletes its own before reaching the
#: server, so a gather that never syncs a list (its discovery failed, or the
#: list left the corpus) can't inherit an earlier gather's confirmation. It says
#: only that the server's epoch matched the local one: a FETCH shortfall (see
#: `ShortFetch`) leaves it in place, because the persist archives the old
#: epoch's mail before replacing it.
VERIFIED_FILE = ".verified-uidvalidity"


def verified_uidvalidity(cache_dir: str) -> Optional[str]:
    """The epoch the server confirmed for `cache_dir` during this gather's sync,
    or `None` if the sync didn't reach the server (or hasn't run)."""
    return _read_epoch(os.path.join(cache_dir, VERIFIED_FILE))


def record_verified(cache_dir: str, epoch: str) -> None:
    """Record that the server confirmed `epoch` for `cache_dir` this gather. A
    write failure leaves the epoch unconfirmed, the safe side for persist."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        write_if_changed(os.path.join(cache_dir, VERIFIED_FILE), epoch)
    except OSError as err:
        # Without this, persist's later "was not confirmed" points at the IMAP
        # server when the cause is the disk.
        log(
            f"could not record the confirmed UIDVALIDITY for {cache_dir}: {err}; "
            "the cloud persist will not replace stored mail for this list",
            level=LogLevel.WARN,
        )


def clear_verified(cache_dir: str) -> None:
    """Drop `cache_dir`'s confirmation, if any."""
    try:
        os.remove(os.path.join(cache_dir, VERIFIED_FILE))
    except FileNotFoundError:
        pass


def forget_verified(wg_name: str) -> None:
    """Drop every confirmation recorded for `wg_name`'s lists, so a new gather
    starts with none."""
    root = os.path.join(get_cache_dir(), "imap-cache", wg_name)
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        list_dir = os.path.join(root, name)
        # A stray file here must not abort the caller (hydrate).
        if os.path.isdir(list_dir):
            clear_verified(list_dir)


def _read_epoch(path: str) -> Optional[str]:
    """The bare-number epoch in `path`, or `None` if it is missing or holds
    anything else."""
    try:
        with open(path, "rb") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return text.decode("ascii") if text.isdigit() else None


def read_uidvalidity(cache_dir: str) -> Optional[str]:
    """The epoch recorded for a list's cache, or `None` if there is no usable
    marker. A marker that isn't a bare number counts as absent, so a corrupt one
    is adopted over rather than read as a changed epoch."""
    return _read_epoch(os.path.join(cache_dir, UIDVALIDITY_FILE))


def _server_uidvalidity(mail: imaplib.IMAP4_SSL) -> Optional[str]:
    """UIDVALIDITY the server reported for the selected folder, or `None` if it
    didn't (or sent something unparseable)."""
    try:
        _, data = mail.response("UIDVALIDITY")
    except (imaplib.IMAP4.error, OSError):
        return None
    raw = data[0] if data else None
    # imaplib's response-code regex is greedy, so trailing text containing a
    # further `]` comes through attached to the number; take just the digits.
    match = re.match(rb"\s*(\d+)(?!\w)", raw) if isinstance(raw, bytes) else None
    return match.group(1).decode() if match else None


def _reconcile_uidvalidity(
    mail: imaplib.IMAP4_SSL, cache_dir: str, verbose: Verbosity
) -> None:
    """Drop the cached messages when the folder's UIDVALIDITY has changed.

    A changed value means the server renumbered the folder, so a cached
    `<uid>.eml` may be a different message from the one now at that UID -- and
    the "already cached" check would then skip fetching the real one. The cache
    is refetched rather than trusted, and the old files move to
    `epoch-<old>/` rather than being deleted: a windowed refetch can't bring
    back mail older than the window. A cache with no recorded epoch (first sync,
    or one that predates the marker) is adopted as-is."""
    current = _server_uidvalidity(mail)
    if current is None:
        log(
            "  server reported no usable UIDVALIDITY; cached messages not checked.",
            verbose,
            level=LogLevel.WARN,
        )
        return
    previous = read_uidvalidity(cache_dir)
    if previous is not None and previous != current:
        archive = os.path.join(cache_dir, f"{EPOCH_DIR_PREFIX}{previous}")
        log(
            f"  UIDVALIDITY changed ({previous} -> {current}); moving the cached "
            f"messages for this list to {os.path.basename(archive)}/ and refetching.",
            verbose,
            level=LogLevel.WARN,
        )
        os.makedirs(archive, exist_ok=True)
        for name in os.listdir(cache_dir):
            if name.endswith(".eml"):
                os.replace(os.path.join(cache_dir, name), os.path.join(archive, name))
    if previous != current:
        write_if_changed(os.path.join(cache_dir, UIDVALIDITY_FILE), current)
    record_verified(cache_dir, current)


class _FolderSelectError(Exception):
    """The IMAP folder for a list couldn't be selected. Almost always a wrong
    or renamed list name rather than a transient fault, so it isn't retried."""


class _FolderFreshness(NamedTuple):
    """Why a windowed search came back empty: how many messages the folder holds
    overall, and the date of its newest one. Lets the caller tell a genuinely
    empty folder from a stalled archive mirror that stopped years before the
    window."""

    total: int
    newest: Optional[datetime]


def _folder_freshness(mail: imaplib.IMAP4_SSL) -> Optional[_FolderFreshness]:
    """Total message count and newest message date for the selected folder.

    Probed only when a search returned nothing, to explain why. Uses the highest
    UID (append order tracks arrival) for the newest date. Best effort: any
    protocol hiccup yields `None` rather than derailing the sync — this is
    diagnostic, not load-bearing.

    `None` means *we could not tell*, and is deliberately distinct from
    `_FolderFreshness(0, None)` — a folder we successfully probed and found
    empty. Collapsing the two would let a hiccup on this throwaway probe render
    as a confident "the folder is empty, check the list name" to the user."""
    try:
        status, data = mail.uid("search", "ALL")
        if status != "OK":
            return None
        if not data or not data[0]:
            return _FolderFreshness(0, None)
        uids = data[0].split()
        status, fetched = mail.uid("fetch", uids[-1], "(INTERNALDATE)")
        if status != "OK":
            return _FolderFreshness(len(uids), None)
        for part in fetched:
            raw = part[0] if isinstance(part, tuple) else part
            if not isinstance(raw, bytes):
                continue
            parsed = imaplib.Internaldate2tuple(raw)
            if parsed:
                return _FolderFreshness(len(uids), datetime(*parsed[:6]))
        return _FolderFreshness(len(uids), None)
    except (imaplib.IMAP4.error, OSError):
        return None


def _imap_ssl_context() -> ssl.SSLContext:
    """A context that verifies the server certificate and hostname.

    `imaplib.IMAP4_SSL` with no `ssl_context` builds an *unverified* one, so
    this is never optional. Without the platform trust store (opted out, or
    unusable here) it is OpenSSL's defaults plus certifi's bundle, which is
    what `IETF_LLM_SYSTEM_TRUST_STORE=off` promises and the only anchors an
    image without `ca-certificates` has.
    """
    context = system_trust_context()
    if context is None:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
    return context


def _imap_sync_attempt(
    list_name: str,
    months: Optional[int],
    cache_dir: str,
    verbose: Verbosity,
    on_progress: Optional[Callable[[int, int], None]],
    isolate: bool = False,
) -> tuple[List[str], int, Optional[_FolderFreshness]]:
    """One IMAP connect -> select -> search -> download pass for a single list.

    Returns `(uids-in-window, newly-downloaded-count, freshness)`. `freshness`
    is probed only when the search came back empty — and is `None` either
    because there was nothing to explain or because the probe itself failed;
    the caller treats an unprobeable folder as unknown, not as empty. Raises
    `_FolderSelectError` when the folder can't be selected (not retryable) and
    `imaplib.IMAP4.error` / `OSError` on a connection-level fault the caller may
    retry; `ShortFetch` (an `IMAP4.error`) when some messages were not fetched."""
    mail = imaplib.IMAP4_SSL(
        IMAP_SERVER, IMAP_PORT, timeout=IMAP_TIMEOUT, ssl_context=_imap_ssl_context()
    )
    try:
        mail.login(IMAP_USER, IMAP_PASS)
        folder = f'"Shared Folders/{list_name}"'
        status, _ = mail.select(folder, readonly=True)
        if status != "OK":
            raise _FolderSelectError(folder)
        _reconcile_uidvalidity(mail, cache_dir, verbose)
        search_criteria = "ALL"
        if months:
            since_date = (datetime.now() - timedelta(days=30 * months)).strftime(
                "%d-%b-%Y"
            )
            search_criteria = f'(SINCE "{since_date}")'
            log(
                f"  searching for messages since {since_date}...",
                verbose,
                level=LogLevel.PROGRESS,
            )
        status, data = mail.uid("search", search_criteria)
        if status != "OK":
            # A non-OK search is a protocol fault, not an empty result — raise
            # so the caller's retry can recover from a transient one.
            raise imaplib.IMAP4.error(f"search returned status {status!r}")
        uids = data[0].split()
        log(
            f"  found {len(uids)} potential messages on '{list_name}'.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        # One directory listing beats a stat() per UID when the search
        # window holds thousands of already-cached messages.
        cached = {n for n in os.listdir(cache_dir) if n.endswith(".eml")}
        missing_uids = [uid for uid in uids if f"{uid.decode()}.eml" not in cached]
        new_count = 0
        if missing_uids:
            try:
                new_count = _download_batches(
                    mail, missing_uids, cache_dir, verbose, on_progress, isolate
                )
            except ShortFetch as short:
                short.uids = [u.decode() for u in uids]
                raise
        # Probe whenever the search came up empty, windowed or not: on an
        # all-history search the probe is a second no-op SEARCH ALL, and its
        # success is what distinguishes a folder we know is empty from one we
        # could not read.
        freshness = _folder_freshness(mail) if not uids else None
        return [u.decode() for u in uids], new_count, freshness
    finally:
        try:
            mail.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _empty_window_message(
    list_name: str, window: str, freshness: Optional[_FolderFreshness]
) -> str:
    """One line explaining an empty windowed sync.

    Reports how much mail the folder holds overall and how recent it is, and
    leaves the interpretation to the reader: nothing anywhere reads as a wrong
    list name, while plenty of mail whose newest is years old reads as a stalled
    IMAP feed (the Web archive keeps receiving mail the IMAP mirror never gets).
    We deliberately don't guess which — the counts say it, and a guess keyed off
    a grace period gets it wrong for lists that are simply quiet.

    A `freshness` of None means the probe couldn't tell us, so the line claims
    nothing beyond the empty window. This message reaches an MCP client, which
    can't see the stderr context a CLI user has — an overconfident "the folder
    is empty, check the list name" off a failed probe would be acted on.
    """
    if freshness is None:
        # The probe failed, so we know only that this window was empty. Say
        # exactly that rather than guessing at a cause.
        return f"Mailing list '{list_name}': no messages in {window}."
    if not freshness.total:
        return (
            f"Mailing list '{list_name}': no messages in {window}; the archive "
            "folder is empty. Check the list name if you expected traffic."
        )
    newest = freshness.newest
    newest_str = newest.strftime("%Y-%m-%d") if newest else "unknown"
    return (
        f"Mailing list '{list_name}': no messages in {window}; the archive "
        f"folder holds {freshness.total} message(s), newest {newest_str}."
    )


def _undeliverable_detail(short: ShortFetch) -> str:
    """The UIDs the server refused to deliver one at a time, for the shortfall
    note; any remainder is a part of the batch the request cap left unexamined."""
    if not short.undeliverable:
        return ""
    shown = ", ".join(short.undeliverable[:20])
    extra = len(short.undeliverable) - 20
    more = f" and {extra} more" if extra > 0 else ""
    unexamined = short.missing - len(short.undeliverable)
    rest = f"; {unexamined} others not isolated" if unexamined > 0 else ""
    return f" (undeliverable UIDs: {shown}{more}{rest})"


def _sync_one_list(
    wg_name: str,
    list_name: str,
    months: Optional[int],
    verbose: Verbosity,
    on_progress: Optional[Callable[[int, int], None]] = None,
    note_fn: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """IMAP-sync a single list. Returns the UIDs (as strings) that fall within
    the search window for downstream processing. Per-list cache lives at
    `imap-cache/<wg>/<list>/`.

    A connection-level failure is retried once (`IMAP_RETRIES`) — these are
    usually a momentary server hiccup, not a permanent fault. A folder that
    won't select is not retried.

    Either way the per-list outcome — how many messages, or why there were none
    — goes to the log *and*, when `note_fn` is given, to the caller's note sink.
    An empty sync is never fatal (a quiet list is a normal thing, and the rest of
    the gather succeeded), so the note is the only way a caller who isn't reading
    stderr — an MCP client polling `gather_status` — can tell what happened."""
    log(
        f"Syncing list '{list_name}' for WG {wg_name} via IMAP...",
        verbose,
        level=LogLevel.STATUS,
    )

    def report(message: str, level: LogLevel) -> None:
        log(message, verbose, level=level)
        if note_fn is not None:
            note_fn(message)

    cache_dir = os.path.join(get_cache_dir(), "imap-cache", wg_name, list_name)
    os.makedirs(cache_dir, exist_ok=True)
    clear_verified(cache_dir)
    window = f"the last {months} month(s)" if months else "all history"
    fetched_before_retry = 0
    for attempt in range(IMAP_RETRIES + 1):
        try:
            uids, new_count, freshness = _imap_sync_attempt(
                list_name,
                months,
                cache_dir,
                verbose,
                on_progress,
                attempt == IMAP_RETRIES,
            )
            new_count += fetched_before_retry
        except _FolderSelectError:
            report(
                f"Mailing list '{list_name}': no such folder on the IETF IMAP "
                "server, which mirrors the archives at mailarchive.ietf.org "
                "under the list's own name. No mail gathered.",
                LogLevel.ERROR,
            )
            return []
        except ShortFetch as short:
            fetched_before_retry += short.written
            if attempt < IMAP_RETRIES:
                log(
                    f"  {short} on '{list_name}'; retrying...",
                    verbose,
                    level=LogLevel.WARN,
                )
                continue
            # What did arrive is cached and threaded; what didn't is missing
            # until a later gather fetches it. The epoch stays confirmed: the
            # server did vouch for it, and the cloud persist archives the old
            # epoch's mail before replacing it, so a partial new epoch loses
            # nothing.
            report(
                f"Mailing list '{list_name}': {short.missing} message(s) in "
                f"{window} could not be fetched after {IMAP_RETRIES + 1} "
                f"attempts{_undeliverable_detail(short)}; they are missing from "
                "this run's threads until a later gather fetches them.",
                LogLevel.ERROR,
            )
            return short.uids
        except (imaplib.IMAP4.error, OSError) as err:
            # A certificate that fails verification will fail again.
            bad_cert = certificate_error(err) is not None
            if attempt < IMAP_RETRIES and not bad_cert:
                log(
                    f"  IMAP error on '{list_name}' ({err}); retrying...",
                    verbose,
                    level=LogLevel.WARN,
                )
                continue
            tried = "" if bad_cert else f" after {IMAP_RETRIES + 1} attempts"
            report(
                f"Mailing list '{list_name}': IMAP sync failed{tried} "
                f"({err}){certificate_hint(err)}; no mail gathered this run.",
                LogLevel.ERROR,
            )
            return []
        if not uids:
            report(_empty_window_message(list_name, window, freshness), LogLevel.WARN)
        else:
            report(
                f"Mailing list '{list_name}': {len(uids)} message(s) in "
                f"{window} ({new_count} new).",
                LogLevel.STATUS,
            )
        return uids
    return []  # unreachable: the loop always returns; satisfies the type checker


def _cached_eml_count(wg_name: str, list_names: List[str]) -> int:
    """How many .eml files the given lists hold. Detects "something arrived"
    without changing `_sync_one_list`'s tested return contract."""
    total = 0
    for list_name in list_names:
        cache_dir = os.path.join(get_cache_dir(), "imap-cache", wg_name, list_name)
        try:
            total += sum(1 for n in os.listdir(cache_dir) if n.endswith(".eml"))
        except OSError:
            continue
    return total


def sync_mailing_list(
    wg_name: str,
    dest_folder: str,
    months: Optional[int] = None,
    extra_lists: Optional[List[str]] = None,
    auto_discover: bool = True,
    verbose: Verbosity = Verbosity.STATUS,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    suppress_raw: bool = False,
    note_fn: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Sync the WG's mailing list(s) via IMAP and cache messages.

    By default, includes the auto-discovered list (looked up from
    Datatracker by WG affinity). `extra_lists` adds further lists
    the WG follows but Datatracker doesn't attribute to it — passed
    in via `--mailing-list` on the CLI. Set `auto_discover=False` to
    skip the Datatracker lookup entirely (used for synthetic / `x-`
    corpora, which have no WG record to look up against). Each list
    keeps its own per-list IMAP cache (`imap-cache/<wg>/<list>/`),
    and the thread-reconstruction walker already picks up every
    `.eml` under `imap-cache/<wg>/` regardless of subdir.

    Returns the list of `raw/mail-archive-<year>.txt` files written.
    Year dumps are merged across all lists — they're for human grep
    / NotebookLM upload, not for indexed retrieval. When `suppress_raw`
    is set those merged dumps are skipped entirely (the per-list IMAP
    `.eml` cache, which is the real efficiency token and the source the
    thread reconstruction reads, is always written); returns []
    accordingly.

    `note_fn`, when given, receives one line per list describing that list's
    outcome (see `_sync_one_list`), plus one for the case where no list is
    configured at all. The gather's own logs already carry these; the notes are
    what makes them visible to a caller reading `gather_status` rather than
    stderr.
    """
    list_names: List[str] = []
    seen: set[str] = set()
    if auto_discover:
        auto = normalize_list_name(get_mailing_list_name(wg_name) or "")
        if auto:
            list_names.append(auto)
            seen.add(auto)
    for raw in extra_lists or []:
        norm = normalize_list_name(raw)
        if norm and norm not in seen:
            list_names.append(norm)
            seen.add(norm)
    if not list_names:
        if not auto_discover:
            # A synthetic / custom corpus that named no list: there was never a
            # mailing-list source to succeed or fail at, so this is not an
            # outcome to report. Note it anyway and every drafts-only corpus
            # hands its client a phantom failure to relay.
            log(
                f"No mailing list for {wg_name} (none specified, and this "
                "corpus has no group to discover one from); skipping mail sync.",
                verbose,
                level=LogLevel.STATUS,
            )
            return []
        message = (
            f"No mailing list configured for {wg_name} (auto-discovery "
            "failed and no --mailing-list specified); skipping mail sync."
        )
        log(message, verbose, level=LogLevel.STATUS)
        if note_fn is not None:
            note_fn(message)
        return []

    # Per-list IMAP sync + UID collection.
    n_before = _cached_eml_count(wg_name, list_names)
    per_list_uids: Dict[str, List[str]] = {}
    for list_name in list_names:
        per_list_cb: Optional[Callable[[int, int], None]] = None
        if on_progress is not None:
            # Bind the list name so the caller can label progress when a WG
            # follows several lists. Default-arg capture avoids the late-binding
            # loop-variable trap.
            def per_list_cb(  # pylint: disable=function-redefined
                done: int, total: int, _name: str = list_name
            ) -> None:
                on_progress(_name, done, total)

        per_list_uids[list_name] = _sync_one_list(
            wg_name,
            list_name,
            months,
            verbose,
            per_list_cb,
            note_fn=note_fn,
        )

    # Per-list year archives, then merge across lists into one file
    # per year so the consumer doesn't have to know which list a
    # message came from at grep time. (Threading uses the .eml files
    # directly and naturally interleaves anyway.) Skipped under
    # suppress_raw: the merged dumps are regenerable, never indexed,
    # and never read by a tool — the .eml cache synced above is what
    # the thread reconstruction reads. Sweep any dumps an earlier
    # non-suppressed gather left behind so a cache migrated to the
    # cloud backend doesn't carry stale raw/ bulk forward (mirrors the
    # .pdf sweep in extract_all_pdfs).
    if suppress_raw:
        for stale in glob.glob(
            os.path.join(raw_dir(dest_folder), "mail-archive-*.txt")
        ):
            try:
                os.remove(stale)
            except OSError:
                pass
        return []
    # No new mail: rebuilding re-parses every cached message to write the same
    # bytes back.
    # Keyed on new mail, not other staleness: narrowing --months keeps wider
    # dumps, re-adding a previously gathered list does not restore it, and a
    # future rendering change does not rebuild existing dumps. These dumps are
    # export-only; new mail or manually removing raw/ triggers a rebuild.
    existing_dumps = glob.glob(os.path.join(raw_dir(dest_folder), "mail-archive-*.txt"))
    if existing_dumps and _cached_eml_count(wg_name, list_names) == n_before:
        log(
            "Mail archives: no new messages; keeping the existing "
            f"{len(existing_dumps)} year dump(s).",
            verbose,
            level=LogLevel.STATUS,
        )
        return []

    combined: Dict[int, List[str]] = {}
    for list_name, uids in per_list_uids.items():
        cache_dir = os.path.join(
            get_cache_dir(),
            "imap-cache",
            wg_name,
            list_name,
        )
        if not uids:
            continue
        yearly = process_cache(cache_dir, uids, verbose)
        for year, content in yearly.items():
            combined.setdefault(year, []).append(content)

    updated_files: List[str] = []
    os.makedirs(raw_dir(dest_folder), exist_ok=True)
    for year, parts in combined.items():
        # Join with the same record separator the per-list processor
        # uses internally so the merged archive looks uniform.
        merged = "\n=====\n\n".join(parts)
        output_file = raw_mail_archive_path(dest_folder, year)
        # write_if_changed both skips an unchanged rewrite and writes
        # atomically (temp + rename), so a crash can't truncate the archive.
        if write_if_changed(output_file, merged):
            updated_files.append(output_file)
    return updated_files


def process_cache(
    cache_dir: str,
    uids: Optional[List[str]] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> Dict[int, str]:
    """Process cached .eml files and return cleaned text grouped by year."""
    log(
        "Processing cached messages...",
        verbose,
        level=LogLevel.STATUS,
    )

    # Get .eml files to process
    if uids:
        eml_files = [f"{uid}.eml" for uid in uids]
    else:
        eml_files = [fname for fname in os.listdir(cache_dir) if fname.endswith(".eml")]
        # Sort them numerically by UID
        eml_files.sort(key=lambda x: int(x.split(".")[0]))

    yearly_content: Dict[int, List[str]] = {}
    count = 0

    for eml_file in eml_files:
        cache_path = os.path.join(cache_dir, eml_file)
        if not os.path.exists(cache_path):
            continue

        with open(cache_path, "rb") as file_handle:
            msg = email.message_from_binary_file(
                file_handle, policy=email.policy.default
            )

        # Extract Year from Date header
        date_header = msg.get("Date")
        year = None
        if date_header:
            try:
                date_dt = email.utils.parsedate_to_datetime(str(date_header))
                year = date_dt.year
            except (ValueError, TypeError, IndexError):
                pass

        if year is None:
            continue

        if year not in yearly_content:
            yearly_content[year] = []

        subject = msg.get("Subject", "(No Subject)")
        from_addr = msg.get("From", "(Unknown Sender)")
        date_val = msg.get("Date", "(Unknown Date)")

        raw_body = extract_text_content(msg)
        cleaned_body = clean_email_text(raw_body)

        if not cleaned_body and subject == "(No Subject)":
            continue

        message_text = (
            f"Date: {date_val}\n"
            f"From: {from_addr}\n"
            f"Subject: {subject}\n\n"
            f"{cleaned_body}\n\n"
            f"{'=' * 80}\n\n"
        )
        yearly_content[year].append(message_text)

        count += 1
        if count % 100 == 0:
            log(f"Processed {count} messages...", verbose, level=LogLevel.PROGRESS)

    log(
        f"Done! Processed {count} messages.",
        verbose,
        level=LogLevel.STATUS,
    )

    return {yr: "".join(contents) for yr, contents in yearly_content.items()}
