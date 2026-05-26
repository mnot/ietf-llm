import email
import email.policy
import email.utils
import html
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.message import EmailMessage, MIMEPart
from typing import List, Optional, Dict

from .utils import LogLevel, Verbosity, get_mailing_list_name, log, get_cache_dir

IMAP_SERVER = "imap.ietf.org"
IMAP_PORT = 993
IMAP_USER = "anonymous"
IMAP_PASS = "mnot+ietf-llm@ietf.org"
BATCH_SIZE = 50


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
) -> int:
    """Download messages in batches and save to cache. Returns count of new messages."""
    new_count = 0
    log(
        f"Downloading {len(missing_uids)} new messages in batches of {BATCH_SIZE}...",
        verbose,
        level=LogLevel.PROGRESS,
    )
    for i in range(0, len(missing_uids), BATCH_SIZE):
        batch = missing_uids[i : i + BATCH_SIZE]
        batch_str = ",".join(b.decode() for b in batch)
        status, msg_data = mail.uid("fetch", batch_str, "(RFC822)")

        if status != "OK" or not msg_data:
            continue

        for item in msg_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue

            # item[0] is the response header, item[1] is the message body
            header = item[0]
            if not isinstance(header, bytes):
                continue
            resp_header = header.decode()

            # Find UID in the response header
            uid_match = re.search(r"UID\s+(\d+)", resp_header)
            if not uid_match:
                continue

            msg_uid = uid_match.group(1)
            cache_file = os.path.join(cache_dir, f"{msg_uid}.eml")
            body = item[1]
            if not isinstance(body, bytes):
                continue

            with open(cache_file, "wb") as file_handle:
                file_handle.write(body)
            new_count += 1

        if new_count > 0:
            log(
                f"Downloaded {new_count}/{len(missing_uids)} new messages...",
                verbose,
                level=LogLevel.PROGRESS,
            )
    return new_count


def sync_mailing_list(
    wg_name: str,
    dest_folder: str,
    months: Optional[int] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Sync mailing list via IMAP and cache messages locally. Returns list of updated files."""
    list_name = get_mailing_list_name(wg_name)
    log(
        f"Syncing list '{list_name}' for WG {wg_name} via IMAP...",
        verbose,
        level=LogLevel.STATUS,
    )

    cache_dir = os.path.join(get_cache_dir(), "imap-cache", wg_name, list_name)
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(IMAP_USER, IMAP_PASS)

        # Quote folder name to handle spaces
        folder = f'"Shared Folders/{list_name}"'
        status, _ = mail.select(folder, readonly=True)
        if status != "OK":
            log(
                f"Error: Could not select IMAP folder '{folder}'",
                verbose,
                level=LogLevel.ERROR,
            )
            return []

        # Determine search criteria
        search_criteria = "ALL"
        if months:
            since_date = (datetime.now() - timedelta(days=30 * months)).strftime(
                "%d-%b-%Y"
            )
            search_criteria = f'(SINCE "{since_date}")'
            log(
                f"Searching for messages since {since_date}...",
                verbose,
                level=LogLevel.PROGRESS,
            )

        status, data = mail.uid("search", search_criteria)
        if status != "OK":
            log("Error: IMAP search failed.", verbose, level=LogLevel.ERROR)
            return []

        uids = data[0].split()
        log(f"Found {len(uids)} potential messages.", verbose, level=LogLevel.PROGRESS)

        # Filter out what we already have in cache
        missing_uids = []
        for uid in uids:
            uid_str = uid.decode()
            cache_file = os.path.join(cache_dir, f"{uid_str}.eml")
            if not os.path.exists(cache_file):
                missing_uids.append(uid)

        new_count = 0
        if missing_uids:
            new_count = _download_batches(mail, missing_uids, cache_dir, verbose)

        mail.logout()
        if new_count > 0:
            log(
                f"Finished downloading {new_count} new messages.",
                verbose,
                level=LogLevel.STATUS,
            )
        else:
            log("No new messages to download.", verbose, level=LogLevel.STATUS)

        # Now process all cached messages into the final archive
        # We only process the UIDs that were found in the search
        yearly_archives = process_cache(cache_dir, [u.decode() for u in uids], verbose)
        updated_files = []
        for year, content in yearly_archives.items():
            output_file = os.path.join(
                dest_folder, f"{wg_name}-mail-archive-{year}.txt"
            )
            # Only write and return if content changed
            if os.path.exists(output_file):
                with open(output_file, "r", encoding="utf-8") as in_fh:
                    if in_fh.read() == content:
                        continue

            with open(output_file, "w", encoding="utf-8") as out_fh:
                out_fh.write(content)
            updated_files.append(output_file)
        return updated_files

    except (imaplib.IMAP4.error, OSError) as err:
        log(f"IMAP Error: {err}", verbose, level=LogLevel.ERROR)
        return []


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
