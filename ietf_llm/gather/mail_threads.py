"""Reconstruct real email threads from the IMAP cache and write one
markdown file per thread to the WG's files/ cache.

This turns the mailing list from a flat year-dump into navigable
conversations. The per-thread .md files are what an LLM agent should
actually read when following a discussion — orders of magnitude more
legible than grepping through `<wg>-mailing-list-YYYY.txt`.

Threading uses the same approach every mail client does:

  1. Parse `Message-Id`, `In-Reply-To`, and `References` from each
     cached `.eml`. These headers are what RFC 5322 designed for the
     job; using them is far more reliable than subject-string matching.
  2. Link each message to its parent (`In-Reply-To` first; fall back
     to the last entry of `References`).
  3. Messages whose parent isn't in our archive (because it was posted
     elsewhere or pre-dates our retention window) become thread roots.
  4. As a safety net, root messages that share a normalised subject
     get merged together. Catches cases where someone "replied" by
     composing a new message without preserving References.

One file per thread:

  <cache>/files/<wg>-thread-<YYYY-MM-DD>-<slug>.md

Inside, messages appear in date order with an outline view at the top,
then one section per message. Quoted text from earlier messages in the
thread is elided so the file is genuinely readable end-to-end.
"""

from __future__ import annotations

import email
import email.errors
import email.policy
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..paths import thread_path, threads_dir
from ..people import Registry
from ..text import _normalize_subject, _parse_date, _short_addr
from ..utils import LogLevel, Verbosity, get_cache_dir, log, write_if_changed
from .mbox import clean_email_text, extract_text_content


@dataclass
class Message:
    """One mailing list message, parsed from a cached .eml."""

    message_id: str  # synthetic for messages with no Message-Id header
    subject: str
    sender: str  # display name (or local-part) from From header
    date: Optional[datetime]
    body: str
    in_reply_to: Optional[str] = None
    references: List[str] = field(default_factory=list)
    parent_id: Optional[str] = None  # resolved after threading
    # IETF mail archive permalink, set by IETF mailman in the
    # `Archived-At:` header (e.g.
    # https://mailarchive.ietf.org/arch/msg/<list>/<token>/). Optional
    # because non-IETF lists won't have it, and some malformed messages
    # may drop the header. When present, we propagate it down to the
    # rendered thread file and ultimately to search hits as a citeable
    # URL for the individual message.
    archived_at: Optional[str] = None


@dataclass
class Thread:
    root: Message
    members: List[Message]  # in date order, root first if dated earliest

    @property
    def subject(self) -> str:
        return _normalize_subject(self.root.subject)

    @property
    def participants(self) -> List[str]:
        return sorted({msg.sender for msg in self.members})

    @property
    def span(self) -> "tuple[Optional[datetime], Optional[datetime]]":
        dates = [msg.date for msg in self.members if msg.date]
        if not dates:
            return (None, None)
        return (min(dates), max(dates))


# --- Parsing ---------------------------------------------------------------


_MSGID_RE = re.compile(r"<[^<>\s]+>")


def _normalize_msgid(value: Optional[str]) -> Optional[str]:
    """Extract <id@host> tokens from a Message-Id-style header value.

    Mail clients are inconsistent here: some put extra commentary
    around the angle-bracketed id; some omit the brackets entirely.
    We normalise to the first `<...>` form if present, else strip.
    """
    if not value:
        return None
    match = _MSGID_RE.search(value)
    if match:
        return match.group(0)
    stripped = value.strip()
    return stripped or None


def _extract_references(value: Optional[str]) -> List[str]:
    """Pull all `<id>` tokens out of a References header in order."""
    if not value:
        return []
    return _MSGID_RE.findall(value)


def parse_eml(path: str, registry: Optional[Registry] = None) -> Optional[Message]:
    """Parse one .eml file. Returns None on unrecoverable read errors.

    If `registry` is supplied, the sender field is set to the canonical
    name resolved through it (so "Mark Nottingham via Datatracker" and
    "Mark Nottingham" both render as "Mark Nottingham"). Without a
    registry we fall back to plain display-name extraction.
    """
    try:
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
    except (OSError, email.errors.MessageError):
        return None

    subject = str(msg.get("Subject") or "(no subject)")
    raw_from = str(msg.get("From") or "")
    if registry is not None:
        sender = registry.canonical_for_email(raw_from) or _short_addr(raw_from)
    else:
        sender = _short_addr(raw_from)
    date = _parse_date(msg.get("Date"))
    msgid = _normalize_msgid(msg.get("Message-Id"))
    if not msgid:
        # Synthetic id keyed off the .eml's path so the message is
        # still identifiable in the graph (with no parent).
        msgid = f"<synthetic-{os.path.basename(path)}@local>"
    in_reply_to = _normalize_msgid(msg.get("In-Reply-To"))
    references = _extract_references(msg.get("References"))
    archived_at = _normalize_archived_at(msg.get("Archived-At"))

    try:
        body = clean_email_text(extract_text_content(msg))
    except Exception:  # pylint: disable=broad-except
        body = ""

    return Message(
        message_id=msgid,
        subject=subject,
        sender=sender,
        date=date,
        body=body,
        in_reply_to=in_reply_to,
        references=references,
        archived_at=archived_at,
    )


def _normalize_archived_at(value: Optional[str]) -> Optional[str]:
    """Extract a URL from an `Archived-At:` header.

    RFC 5064 allows the URL to be enclosed in `<...>` brackets, and some
    mail clients wrap it in angle brackets even when the spec doesn't
    strictly require them. We unwrap exactly that — anything else
    (commentary, multiple URLs) we pass through unchanged rather than
    risk producing a broken URL.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    # A bare scheme like "http://" or "https://" is a useful sanity
    # check — without it we're probably looking at garbage. Don't be
    # stricter than this; archive URL schemes vary.
    if "://" not in text:
        return None
    return text


# --- Quote elision ---------------------------------------------------------


#: An Outlook "-----Original Message-----" separator.
_ORIGINAL_MSG_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE)

#: The verb-tail of an attribution line in any language we've seen in the
#: corpus. The line ends in `<verb>[ <name>]?<optional whitespace>:`.
#: German `schrieb` and Dutch `schreef` carry a name between verb and colon
#: (`schrieb Peter Gutmann:`); the others put the verb immediately before
#: the colon.
_ATTRIBUTION_VERB_RE = re.compile(
    r"(?:"
    r"wrote|writes"  # English
    r"|a écrit"  # French
    r"|ha scritto"  # Italian
    r"|escreveu"  # Portuguese
    r"|escribió"  # Spanish
    r"|schrieb [^:]+"  # German "schrieb <Name>"
    r"|geschrieben"  # German alt
    r"|schreef [^:]+"  # Dutch "schreef <Name>"
    r")\s*:\s*$"
)

#: A "<word> <date>" prefix that marks the line as a quoted-reply
#: attribution rather than prose. Each word is the language's
#: equivalent of English "On" in `On <date>, <Name> wrote:`.
_DATE_PREFIX_RE = re.compile(r"^(?:On|Le|Il|Am|Op|Em|El|W dniu)\s+\S.*\d")

#: Bare wrapped tails of a multi-line attribution, e.g. when `On <date>,
#: <Name> <addr>\nwrote:` wraps to its own line.
_BARE_TAIL_ATTRIBUTIONS = frozenset({"wrote:", "writes:"})


def _is_attribution(stripped: str) -> bool:
    """A no-`>` attribution line that introduces a quoted reply trail —
    `On <date>, <Name> <addr> wrote:` and its non-English equivalents
    (`Am … schrieb <Name>:`, `Le … a écrit :`, etc.). Needs a quote-source
    signal (an address, or a date prefix) so it doesn't fire on prose like
    'the authors wrote:'."""
    if not _ATTRIBUTION_VERB_RE.search(stripped):
        return False
    if "@" in stripped or "<" in stripped:
        return True
    if _DATE_PREFIX_RE.match(stripped):
        return True
    return stripped in _BARE_TAIL_ATTRIBUTIONS


def _has_outlook_header_block(lines: List[str], i: int) -> bool:
    """A `From:` line that opens an Outlook/Exchange quoted-header block —
    a `Sent:`/`Date:` and a `Subject:` within the next few lines."""
    window = [ln.strip().lower() for ln in lines[i : i + 6]]
    has_sent = any(w.startswith(("sent:", "date:")) for w in window)
    has_subject = any(w.startswith("subject:") for w in window)
    return has_sent and has_subject


def _quoted_trail_start(lines: List[str]) -> Optional[int]:
    """Index of the first line that begins a no-`>` quoted reply trail
    (Outlook / Exchange / Apple top-post markers), or None. Everything
    from there to the end of a top-posted message is the prior thread
    re-quoted verbatim."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _ORIGINAL_MSG_RE.match(stripped):
            return i
        if stripped.lower().startswith("from:") and _has_outlook_header_block(lines, i):
            return i
        if _is_attribution(stripped):
            return i
    return None


def elide_quotes(text: str, keep_threshold: int = 2) -> str:
    """Collapse quoted reply trails so a thread file is readable.

    Two shapes are handled. `>`-prefixed quote runs longer than
    `keep_threshold` collapse to a single `> [N lines elided]` marker.
    And the no-`>` quoting that Outlook / Exchange / Apple Mail produce —
    a `-----Original Message-----` separator, a `From:`/`Sent:`/`Subject:`
    header block, or an `On … wrote:` attribution, after which the prior
    thread is pasted verbatim with no prefix — is elided from the first
    such boundary to the end of the (top-posted) message. That trail is
    the earlier messages, which the thread file already carries as their
    own sections, so nothing is lost.
    """
    lines = text.splitlines()
    trail_marker: Optional[str] = None
    cut = _quoted_trail_start(lines)
    if cut is not None:
        trail_marker = f"[{len(lines) - cut} lines of quoted reply trail elided]"
        lines = lines[:cut]

    out: List[str] = []
    run: List[str] = []
    for line in lines:
        if line.lstrip().startswith(">"):
            run.append(line)
            continue
        if run:
            if len(run) > keep_threshold:
                out.append(f"> [{len(run)} quoted lines elided]")
            else:
                out.extend(run)
            run = []
        out.append(line)
    if run:
        if len(run) > keep_threshold:
            out.append(f"> [{len(run)} quoted lines elided]")
        else:
            out.extend(run)
    if trail_marker is not None:
        while out and not out[-1].strip():
            out.pop()
        out.append(trail_marker)
    return "\n".join(out)


# --- Thread building -------------------------------------------------------


def _walk_imap_cache(wg: str) -> List[str]:
    """Return absolute paths of every .eml in the WG's IMAP cache."""
    root = os.path.join(get_cache_dir(), "imap-cache", wg)
    if not os.path.isdir(root):
        return []
    out: List[str] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".eml"):
                out.append(os.path.join(dirpath, name))
    return out


def build_threads(wg: str, registry: Optional[Registry] = None) -> List[Thread]:
    """Reconstruct threads from the WG's IMAP cache.

    Pass `registry` (from `people.build_registry()`) to render senders
    using consolidated canonical names instead of raw display strings.
    """
    msgs: List[Message] = []
    for path in _walk_imap_cache(wg):
        parsed = parse_eml(path, registry=registry)
        if parsed is not None:
            msgs.append(parsed)
    if not msgs:
        return []

    by_id: Dict[str, Message] = {msg.message_id: msg for msg in msgs}

    # Resolve parent via In-Reply-To, falling back to References.
    for msg in msgs:
        parent: Optional[str] = None
        if msg.in_reply_to and msg.in_reply_to in by_id:
            parent = msg.in_reply_to
        else:
            for ref in reversed(msg.references):
                if ref in by_id:
                    parent = ref
                    break
        msg.parent_id = parent

    # Roots are messages whose parent isn't in our archive.
    # Safety net: if two roots share a normalised subject they're
    # almost certainly the same conversation (someone replied as new).
    subject_root: Dict[str, Message] = {}
    for msg in msgs:
        if msg.parent_id is not None:
            continue
        key = _normalize_subject(msg.subject).lower()
        if not key:
            continue
        existing = subject_root.get(key)
        if existing is None:
            subject_root[key] = msg
        else:
            # Older message becomes the root; younger one parents to it.
            if msg.date and existing.date and msg.date < existing.date:
                existing.parent_id = msg.message_id
                subject_root[key] = msg
            else:
                msg.parent_id = existing.message_id

    # Collect descendants per root.
    children: Dict[str, List[Message]] = defaultdict(list)
    for msg in msgs:
        if msg.parent_id is not None:
            children[msg.parent_id].append(msg)

    threads: List[Thread] = []
    for root in subject_root.values():
        members = _collect_subtree(root, children)
        # Order chronologically; messages without a date sink to the end.
        members.sort(key=lambda mm: mm.date.timestamp() if mm.date else float("inf"))
        threads.append(Thread(root=root, members=members))
    return threads


def _collect_subtree(
    root: Message, children: Dict[str, List[Message]]
) -> List[Message]:
    out = [root]
    stack = list(children.get(root.message_id, []))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(children.get(node.message_id, []))
    return out


# --- Per-thread markdown files ---------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def thread_slug(subject: str, first_date_iso: Optional[str]) -> str:
    """Stable, readable filename slug for a thread.

    Public so other modules (e.g. the digest table) can produce the
    same slug from the same inputs without re-importing the regex.
    """
    body = _normalize_subject(subject) or "no-subject"
    slug = _SLUG_RE.sub("-", body.lower()).strip("-")
    if len(slug) > 60:
        slug = slug[:60].rstrip("-")
    date_prefix = first_date_iso or "undated"
    return f"{date_prefix}-{slug}" if slug else date_prefix


def _thread_slug_for(thread: Thread) -> str:
    first_date = thread.span[0]
    iso = first_date.strftime("%Y-%m-%d") if first_date else None
    return thread_slug(thread.root.subject, iso)


def _name_with_role(sender: str, registry: Optional["Registry"]) -> str:
    """Append a short role tag if the registry knows one for this sender.

    "Mark Nottingham" → "Mark Nottingham (Chair)" when the registry has
    Mark as a WG chair. Returns the input unchanged when no registry is
    passed or the sender carries no role we surface. Used in both the
    outline bullets and the message section headers so role attribution
    is visible wherever an author name appears.
    """
    if registry is None or not sender:
        return sender
    tag = registry.role_tag(sender)
    return f"{sender} ({tag})" if tag else sender


def _format_msg_date(msg: "Message") -> str:
    """Render `msg.date` for the per-thread file's section headers.

    Always emits UTC ("YYYY-MM-DD HH:MM") regardless of the original
    Date header's timezone. This matters: the chunker re-parses
    these timestamps to build `chunks.chunk_date` for chronological
    sorting (read_topic, sort=date). If we render in the message's
    local timezone, a 15:00 PT message looks identical on the page
    to a 15:00 UTC message — and `read_topic` mis-orders them by
    up to 12 hours. Naive datetimes are treated as already-UTC.
    """
    if msg.date is None:
        return "(undated)"
    aware = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _build_outline(thread: Thread, registry: Optional["Registry"] = None) -> str:
    """A short bulleted list of (date, sender, subject) for navigation."""
    lines = []
    for idx, msg in enumerate(thread.members, 1):
        when = _format_msg_date(msg)
        lines.append(f"- **[{idx}]** {when} — {_name_with_role(msg.sender, registry)}")
    return "\n".join(lines)


def _render_thread(thread: Thread, registry: Optional["Registry"] = None) -> str:
    """Build the per-thread markdown document."""
    first, last = thread.span
    span_text = (
        f"{first.strftime('%Y-%m-%d')} → {last.strftime('%Y-%m-%d')}"
        if first and last
        else "(undated)"
    )
    parts: List[str] = []
    parts.append(f"# {_normalize_subject(thread.root.subject)}\n")
    parts.append(f"**Span:** {span_text}  ")
    parts.append(f"**Messages:** {len(thread.members)}  ")
    # Per-participant message counts + role + affiliation tags. Lets a
    # consumer see "plurality vs vocal minority" at a glance, plus
    # implementer signal (who's shipping code under which org).
    # Formats:
    #   `Name (12)`                          — no role, no affiliation
    #   `Name (Chair, 12)`                   — role only
    #   `Name (Foo Inc, 12)`                 — affiliation only
    #   `Name (Chair · Foo Inc, 12)`         — both
    # Affiliation comes from drafts the person has authored, NOT from
    # email domain — see Person.affiliations.
    msg_counts: Dict[str, int] = {}
    for msg in thread.members:
        msg_counts[msg.sender] = msg_counts.get(msg.sender, 0) + 1
    # Sort by message count descending, then name for stability.
    participants_detail: List[str] = []
    for sender, count in sorted(msg_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        role = registry.role_tag(sender) if registry else None
        aff = registry.affiliation_tag(sender) if registry else None
        bits: List[str] = []
        if role and aff:
            bits.append(f"{role} · {aff}")
        elif role:
            bits.append(role)
        elif aff:
            bits.append(aff)
        bits.append(str(count))
        participants_detail.append(f"{sender} ({', '.join(bits)})")
    parts.append(
        f"**Participants ({len(msg_counts)}):** " + ", ".join(participants_detail)
    )
    parts.append("")
    parts.append("## Outline\n")
    parts.append(_build_outline(thread, registry))
    parts.append("")
    parts.append("## Messages\n")
    for idx, msg in enumerate(thread.members, 1):
        when = _format_msg_date(msg)
        header = f"### [{idx}] {when} — {_name_with_role(msg.sender, registry)}"
        # Note reply linkage when available.
        if msg.parent_id:
            for p_idx, candidate in enumerate(thread.members, 1):
                if candidate.message_id == msg.parent_id:
                    header += f" (reply to [{p_idx}])"
                    break
        parts.append(header)
        parts.append("")
        parts.append(f"_Subject:_ {msg.subject}")
        if msg.archived_at:
            # IETF archive permalink for the individual message — chunker
            # picks this up and stamps the chunk so search hits surface
            # a citeable URL alongside the file path. Italicised inline
            # form mirrors `_Subject:_` so the file stays human-readable.
            parts.append(f"_Archived-At:_ {msg.archived_at}")
        parts.append("")
        body = elide_quotes(msg.body or "")
        # Single trailing newline; the .splitlines() in elide_quotes
        # already strips the implicit one.
        parts.append(body)
        parts.append("")
    return "\n".join(parts) + "\n"


def write_thread_files(
    wg: str,
    cache_dir: str,
    registry: Optional[Registry] = None,
    verbose: Verbosity = Verbosity.STATUS,
) -> List[str]:
    """Build threads and write one markdown file per thread.

    Returns the list of file paths created. Files are named
    `<wg>-thread-<YYYY-MM-DD>-<slug>.md` and live in `cache_dir`
    (the WG's files/ subdir). Pre-existing thread files are removed
    before writing so a re-gather cleanly reflects the current state.

    Pass `registry` to render senders with consolidated canonical names.
    """
    log("Reconstructing mailing list threads...", verbose, level=LogLevel.STATUS)
    threads = build_threads(wg, registry=registry)
    if not threads:
        return []

    out_dir = threads_dir(cache_dir)
    os.makedirs(out_dir, exist_ok=True)

    # Write-if-changed (NOT wipe-and-rewrite): a byte-identical
    # re-render leaves the file untouched, avoiding needless I/O and
    # mtime churn. (The embedder keys its skip on content hash, so an
    # unchanged re-render is a no-op for embedding regardless.)
    all_paths: List[str] = []
    changed: List[str] = []
    expected: set[str] = set()
    used_slugs: Dict[str, int] = {}
    for thread in threads:
        slug = _thread_slug_for(thread)
        # Disambiguate collisions deterministically.
        used_slugs[slug] = used_slugs.get(slug, 0) + 1
        if used_slugs[slug] > 1:
            slug = f"{slug}-{used_slugs[slug]}"
        path = thread_path(cache_dir, slug)
        expected.add(os.path.basename(path))
        all_paths.append(path)
        if write_if_changed(path, _render_thread(thread, registry)):
            changed.append(path)

    # Remove orphans — thread files from a previous gather whose thread
    # no longer exists (e.g. fell out of the --months window).
    removed = 0
    for name in os.listdir(out_dir):
        if name.endswith(".md") and name not in expected:
            try:
                os.remove(os.path.join(out_dir, name))
                removed += 1
            except OSError:
                pass

    log(
        f"Thread files: {len(all_paths)} current "
        f"({len(changed)} written / changed, {removed} removed)",
        verbose,
        level=LogLevel.STATUS,
    )
    return all_paths
