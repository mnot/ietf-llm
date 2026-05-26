"""Identity normalisation: a registry of WG participants merged across
mailing list addresses and GitHub logins.

The motivation is that a single person shows up under many surface
forms — `Mark Nottingham <mnot@mnot.net>`, `Mark Nottingham via
Datatracker <noreply@ietf.org>`, `mnot=40mnot.net@dmarc.ietf.org`
(DMARC-rewritten), `mnot` (their GitHub login) — and an LLM reading
the corpus without normalisation has to figure out these are all
the same actor. We do that mapping up front and surface a single
canonical display name in the threads, digests, and people file.

What gets normalised:

  - DMARC mailing-list munging: `<local>=40<domain>@dmarc.ietf.org`
    → `<local>@<domain>` (the `=40` is `@` percent-encoded).
  - Relay From-addresses (`noreply@ietf.org`, `noreply@datatracker.ietf.org`,
    `*@mailman3.ietf.org`): the real sender is in the display name, so we
    key by display name rather than the fake address.
  - Display-name suffixes: ` via Datatracker`, ` (IETF)`, etc.
  - Multi-email same person: when two emails share a display name, they
    merge into one Person.
  - GitHub login ↔ email local-part: when a GitHub login appears as the
    local-part of any of a Person's emails, we link them. Conservative
    heuristic; we don't try to infer linkage from name similarity alone.

What we don't do (yet):

  - Cross-WG dedup. The registry is per-WG.
  - User-provided overrides (manual "these two are the same"). The
    registry is recomputed from cache each gather; no persistence yet.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from .text import _parse_date
from .utils import LogLevel, Verbosity, get_cache_dir, log


# --- Person model ----------------------------------------------------------


@dataclass
class Person:
    """One actor in the WG, with all the surface forms we've seen."""

    canonical_name: str
    emails: Set[str] = field(default_factory=set)
    github_logins: Set[str] = field(default_factory=set)
    raw_from_headers: Set[str] = field(default_factory=set)
    # Datatracker role labels held in this WG ("Chair", "Area Director",
    # "Tech Advisor", "Secretary", …). Empty for participants without a
    # formal role.
    roles: Set[str] = field(default_factory=set)
    # Drafts / RFCs this person is listed as an author of (basename
    # without version suffix). Editor status, if any, lives in
    # `edited_documents` so callers can distinguish.
    authored_documents: Set[str] = field(default_factory=set)
    edited_documents: Set[str] = field(default_factory=set)
    message_count: int = 0
    issue_count: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None

    def touch_date(self, date: Optional[datetime]) -> None:
        if date is None:
            return
        if self.first_seen is None or date < self.first_seen:
            self.first_seen = date
        if self.last_seen is None or date > self.last_seen:
            self.last_seen = date


# --- Normalisation helpers -------------------------------------------------


_DMARC_RE = re.compile(
    r"^([^@\s]+)=40([^@\s]+)@dmarc\.ietf\.org$", re.IGNORECASE
)

_RELAY_DOMAINS = {
    "datatracker.ietf.org",
    "ietf.org",
    "mailman3.ietf.org",
}

_NAME_SUFFIX_RE = re.compile(
    r"\s+(?:via\s+Datatracker|\(IETF\)|via\s+Mailman.*)$", re.IGNORECASE
)

# Display order for the leadership list: chairs first, then ADs,
# advisors, secretaries, then anything else alphabetically.
_LEADERSHIP_ROLE_ORDER = {
    "Chair": 0,
    "Area Director": 1,
    "Tech Advisor": 2,
    "Secretary": 3,
}



def _normalise_email(address: str) -> Optional[str]:
    """Resolve munged / relayed addresses to the underlying mailbox.

    Returns None if the address is a pure relay (no recoverable mailbox);
    callers should fall back to display-name-only identity for those.
    """
    if not address:
        return None
    address = address.strip().lower()
    # DMARC rewrite: `local=40domain@dmarc.ietf.org`.
    match = _DMARC_RE.match(address)
    if match:
        return f"{match.group(1)}@{match.group(2)}"
    # Pure relay: the address tells us nothing about who sent it.
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    local = address.split("@", 1)[0] if "@" in address else ""
    if domain in _RELAY_DOMAINS and local in {"noreply", "no-reply"}:
        return None
    return address


def _normalise_name(display: str) -> str:
    """Strip trailing relay-flavour suffixes from a display name."""
    return _NAME_SUFFIX_RE.sub("", display).strip().strip('"')


def parse_from_header(value: str) -> "tuple[Optional[str], Optional[str]]":
    """Extract (canonical display name, normalised email) from a From header.

    Either component can be None. Returns (None, None) if the header
    yields nothing useful.
    """
    if not value:
        return (None, None)
    name, addr = email.utils.parseaddr(value)
    name = _normalise_name(name) if name else ""
    norm_email = _normalise_email(addr) if addr else None
    return (name or None, norm_email)


# --- Registry --------------------------------------------------------------


class Registry:
    """Collect, dedup, and resolve identities for a Working Group."""

    def __init__(self) -> None:
        self.persons: List[Person] = []
        self._by_email: Dict[str, Person] = {}
        self._by_name: Dict[str, Person] = {}
        self._by_github: Dict[str, Person] = {}

    # ----- ingestion -------------------------------------------------------

    def add_email_message(
        self, raw_from: str, date: Optional[datetime]
    ) -> Optional[Person]:
        """Record one mailing list message. Returns the resolved Person."""
        name, addr = parse_from_header(raw_from)
        if not name and not addr:
            return None

        # 1. Look up by email; that's the strongest signal.
        person = self._by_email.get(addr) if addr else None
        # 2. Fall back to display name.
        if person is None and name:
            person = self._by_name.get(name.lower())

        if person is None:
            # Brand new actor.
            person = Person(canonical_name=name or (addr or "unknown"))
            self.persons.append(person)

        # Update what we know about them.
        if name and not _looks_like_email(person.canonical_name):
            # If we previously only had an email-ish "name", and now have
            # a real human name, upgrade.
            if _looks_like_email(person.canonical_name) or person.canonical_name == addr:
                person.canonical_name = name
        elif name and _looks_like_email(person.canonical_name):
            person.canonical_name = name

        if addr:
            person.emails.add(addr)
            self._by_email[addr] = person
        if name:
            self._by_name[name.lower()] = person
        if raw_from:
            person.raw_from_headers.add(raw_from)
        person.message_count += 1
        person.touch_date(date)
        return person

    def add_github_author(
        self, login: str, when: Optional[datetime] = None
    ) -> Optional[Person]:
        """Record one GitHub-authored issue or comment. Returns Person."""
        if not login:
            return None
        login = login.strip()
        person = self._by_github.get(login)
        if person is None:
            # Heuristic link: if any existing Person has an email whose
            # local-part matches the GitHub login, they're almost
            # certainly the same actor.
            for candidate in self.persons:
                for em in candidate.emails:
                    if em.split("@", 1)[0] == login.lower():
                        person = candidate
                        break
                if person is not None:
                    break
        if person is None:
            # GitHub-only actor; canonical name is the login.
            person = Person(canonical_name=login)
            self.persons.append(person)

        person.github_logins.add(login)
        person.issue_count += 1
        self._by_github[login] = person
        person.touch_date(when)
        return person

    def add_document_author(
        self,
        name: str,
        email_address: Optional[str],
        document: str,
        is_editor: bool = False,
    ) -> Optional[Person]:
        """Record `name` as an author of `document` (a draft/RFC basename).

        Merges by email then by name with the existing registry, same
        way add_datatracker_role does. The document basename is stored
        without the version suffix (`draft-ietf-aipref-vocab` not
        `…-06`) so multiple versions don't multiply the role count.
        """
        if not name and not email_address:
            return None
        norm_email = _normalise_email(email_address) if email_address else None

        person = self._by_email.get(norm_email) if norm_email else None
        if person is None and name:
            person = self._by_name.get(name.lower())
        if person is None:
            person = Person(canonical_name=name or (norm_email or "unknown"))
            self.persons.append(person)

        # Draft author lists are the most carefully maintained source
        # of canonical-name spellings (chairs review them); use as the
        # authoritative form.
        if name:
            person.canonical_name = name
            self._by_name[name.lower()] = person
        if norm_email:
            person.emails.add(norm_email)
            self._by_email[norm_email] = person
        if is_editor:
            person.edited_documents.add(document)
        else:
            person.authored_documents.add(document)
        return person

    def add_datatracker_role(
        self,
        name: str,
        email_address: Optional[str],
        label: str,
    ) -> Optional[Person]:
        """Record a formal WG role (Chair, Area Director, …).

        We try to merge the role onto an existing Person — first by
        email, then by canonical name — before creating a new actor.
        This catches the common case where a chair is already in the
        registry from their mailing-list activity.
        """
        if not name and not email_address:
            return None
        norm_email = _normalise_email(email_address) if email_address else None

        person = self._by_email.get(norm_email) if norm_email else None
        if person is None and name:
            person = self._by_name.get(name.lower())
        if person is None:
            person = Person(canonical_name=name or (norm_email or "unknown"))
            self.persons.append(person)

        # Datatracker's display name is canonical — prefer it over
        # whatever the mailing list rendered. ("Mark Nottingham" can
        # appear with diacritics, middle initials, etc.)
        if name:
            person.canonical_name = name
            self._by_name[name.lower()] = person
        if norm_email:
            person.emails.add(norm_email)
            self._by_email[norm_email] = person
        person.roles.add(label)
        return person

    # ----- resolution ------------------------------------------------------

    def canonical_for_email(self, raw_from: str) -> Optional[str]:
        """Resolve any From-header value to its canonical display name."""
        name, addr = parse_from_header(raw_from)
        if addr and addr in self._by_email:
            return self._by_email[addr].canonical_name
        if name and name.lower() in self._by_name:
            return self._by_name[name.lower()].canonical_name
        return name

    def canonical_for_github(self, login: str) -> Optional[str]:
        person = self._by_github.get(login.strip())
        return person.canonical_name if person else login

    # ----- iteration -------------------------------------------------------

    def all_persons(self) -> List[Person]:
        return list(self.persons)

    def leadership(self) -> List[Person]:
        """Persons holding any formal WG role, sorted by role then name."""
        return sorted(
            (p for p in self.persons if p.roles),
            key=lambda p: (_LEADERSHIP_ROLE_ORDER.get(
                next(iter(sorted(p.roles))), 99
            ), p.canonical_name),
        )


def _looks_like_email(name: str) -> bool:
    return "@" in name


# --- Builders --------------------------------------------------------------


def build_registry(wg: str, verbose: Verbosity = Verbosity.STATUS) -> Registry:
    """Build a Registry by scanning IMAP, GitHub, Datatracker, and drafts.

    The Datatracker step is best-effort: if the network call fails or
    the WG isn't recognised, roles are silently omitted and the rest
    of the registry stands.
    """
    registry = Registry()
    _ingest_mail(wg, registry, verbose)
    _ingest_github(wg, registry, verbose)
    _ingest_datatracker_roles(wg, registry, verbose)
    _ingest_draft_authors(wg, registry, verbose)
    log(
        f"Identity registry: {len(registry.persons)} distinct actors "
        f"({sum(1 for p in registry.persons if p.roles)} with formal roles, "
        f"{sum(1 for p in registry.persons if p.authored_documents or p.edited_documents)} "
        "document authors)",
        verbose,
        level=LogLevel.STATUS,
    )
    return registry


def _ingest_mail(wg: str, registry: Registry, verbose: Verbosity) -> None:
    """Walk the IMAP cache and feed every From header through the registry."""
    # Done inline (not via mail_threads.parse_eml) because we need every
    # message — not just ones that thread cleanly — and we want minimal
    # parsing (no body extraction).
    # pylint: disable=import-outside-toplevel,redefined-outer-name
    import email as _email_mod
    import email.policy

    imap = os.path.join(get_cache_dir(), "imap-cache", wg)
    if not os.path.isdir(imap):
        return
    count = 0
    for dirpath, _, filenames in os.walk(imap):
        for name in filenames:
            if not name.endswith(".eml"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as fh:
                    msg = _email_mod.message_from_binary_file(
                        fh, policy=email.policy.default
                    )
            except Exception:  # pylint: disable=broad-except
                continue
            from_h = str(msg.get("From") or "")
            date = _parse_date(msg.get("Date"))
            registry.add_email_message(from_h, date)
            count += 1
    log(f"  ingested {count} mail messages", verbose, level=LogLevel.PROGRESS)


def _ingest_github(wg: str, registry: Registry, verbose: Verbosity) -> None:
    """Read each <wg>-github-*.json archive and feed issue authors."""
    cache_dir = os.path.join(get_cache_dir(), wg, "files")
    if not os.path.isdir(cache_dir):
        return
    count = 0
    for name in os.listdir(cache_dir):
        if not (name.startswith(f"{wg}-github-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for issue in data.get("issues") or []:
            registry.add_github_author(
                issue.get("author") or "",
                _maybe_iso(issue.get("createdAt") or issue.get("updatedAt")),
            )
            for comment in issue.get("comments") or []:
                registry.add_github_author(
                    comment.get("author") or "",
                    _maybe_iso(comment.get("createdAt")),
                )
            count += 1
    log(f"  ingested authors from {count} issues", verbose, level=LogLevel.PROGRESS)


def _ingest_datatracker_roles(
    wg: str, registry: Registry, verbose: Verbosity
) -> None:
    """Fetch chairs/ADs/advisors from Datatracker and add to the registry."""
    # Lazy import: datatracker.py pulls `requests`; keep it out of the
    # cold-path hot loop and isolate failure to this stage.
    from .gather.datatracker import fetch_wg_roles  # pylint: disable=import-outside-toplevel

    for role in fetch_wg_roles(wg, verbose=verbose):
        registry.add_datatracker_role(role.name, role.email, role.label)


def _ingest_draft_authors(
    wg: str, registry: Registry, verbose: Verbosity
) -> None:
    """Parse the Authors' Addresses section of each draft / RFC in the cache.

    Stable name spellings (taken from the document front-matter that
    the chairs review) override any earlier mailing-list-derived form.
    """
    # pylint: disable=import-outside-toplevel
    from .gather.draft_authors import latest_draft_paths, parse_authors
    from .utils import get_wg_file_cache_dir

    cache_dir = get_wg_file_cache_dir(wg)
    count = 0
    for path in latest_draft_paths(cache_dir):
        basename = os.path.basename(path)
        # Strip the version suffix so "draft-…-06.txt" and "…-05.txt"
        # collapse to the same logical document.
        doc_id = re.sub(r"-\d+\.txt$|\.txt$", "", basename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for author in parse_authors(text):
            registry.add_document_author(
                author.name,
                author.email,
                document=doc_id,
                is_editor=author.is_editor,
            )
            count += 1
    log(
        f"  ingested {count} author records from drafts/RFCs",
        verbose,
        level=LogLevel.PROGRESS,
    )


def _maybe_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- _people.md digest -----------------------------------------------------


def write_people_digest(
    wg: str,
    cache_dir: str,
    registry: Registry,
    verbose: Verbosity = Verbosity.STATUS,
) -> Optional[str]:
    """Emit `<wg>-_people.md` describing every distinct actor.

    Three sections:
      - linked: appear on both the mailing list AND GitHub
      - mail-only: have at least one mail message, no GitHub link
      - github-only: GitHub login but no email signal
    """
    persons = registry.all_persons()
    if not persons:
        return None

    linked, mail_only, gh_only = _bucket_persons(persons)

    out_path = os.path.join(cache_dir, f"{wg}-_people.md")
    leaders = registry.leadership()
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {wg}: participants\n\n")
        fh.write(
            f"_{len(persons)} distinct actors. Multiple surface forms "
            "(addresses, DMARC-rewritten variants, GitHub logins) for the "
            "same person are consolidated — `Top senders` columns and "
            "issue authors elsewhere in the corpus use these canonical "
            "names._\n\n"
        )

        # Formal WG leadership comes from Datatracker. Surfaced first
        # because "who runs this WG" is usually what the reader wants
        # to know before scrolling through 100+ participants.
        if leaders:
            fh.write(f"## Working Group leadership ({len(leaders)})\n\n")
            fh.write("| Role | Name | Email |\n|---|---|---|\n")
            for person in leaders:
                roles_text = ", ".join(sorted(person.roles))
                primary_email = next(iter(sorted(person.emails)), "")
                fh.write(
                    f"| {roles_text} | {person.canonical_name} | "
                    f"{primary_email} |\n"
                )
            fh.write("\n")

        # Document authors — surfaced next because "who wrote this
        # draft" is the second question after "who runs the WG".
        authors = [
            p for p in persons
            if p.authored_documents or p.edited_documents
        ]
        if authors:
            fh.write(f"## Document authors / editors ({len(authors)})\n\n")
            fh.write("| Name | Documents | Email |\n|---|---|---|\n")
            authors.sort(key=lambda p: p.canonical_name.lower())
            for person in authors:
                primary_email = next(iter(sorted(person.emails)), "")
                parts = sorted(person.authored_documents) + [
                    f"{d} (ed.)" for d in sorted(person.edited_documents)
                ]
                docs = ", ".join(parts)
                fh.write(
                    f"| {person.canonical_name} | {docs} | {primary_email} |\n"
                )
            fh.write("\n")

        if linked:
            fh.write(
                f"## Active on both mailing list and GitHub ({len(linked)})\n\n"
            )
            _write_actor_table(
                fh,
                linked,
                columns=("Name", "Roles", "Emails", "GitHub", "Msgs", "Issues"),
            )
        if mail_only:
            fh.write(f"## Mailing list only ({len(mail_only)})\n\n")
            _write_actor_table(
                fh,
                mail_only,
                columns=("Name", "Roles", "Emails", "Msgs", "First", "Last"),
            )
        if gh_only:
            fh.write(f"## GitHub only ({len(gh_only)})\n\n")
            _write_actor_table(
                fh, gh_only, columns=("Name", "GitHub", "Issues"),
            )

    log(
        f"Wrote people digest: {len(linked)} linked, "
        f"{len(mail_only)} mail-only, {len(gh_only)} github-only",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


def _bucket_persons(
    persons: Iterable[Person],
) -> "tuple[List[Person], List[Person], List[Person]]":
    linked: List[Person] = []
    mail_only: List[Person] = []
    gh_only: List[Person] = []
    for person in persons:
        has_mail = bool(person.emails) or person.message_count > 0
        has_github = bool(person.github_logins)
        if has_mail and has_github:
            linked.append(person)
        elif has_mail:
            mail_only.append(person)
        elif has_github:
            gh_only.append(person)
    # Sort by activity: most active first within each bucket.
    linked.sort(key=lambda p: -(p.message_count + p.issue_count))
    mail_only.sort(key=lambda p: -p.message_count)
    gh_only.sort(key=lambda p: -p.issue_count)
    return linked, mail_only, gh_only


def _write_actor_table(
    fh: Any, persons: List[Person], columns: Iterable[str]
) -> None:
    columns = list(columns)
    fh.write("| " + " | ".join(columns) + " |\n")
    fh.write("|" + "|".join("---" for _ in columns) + "|\n")
    for person in persons:
        row = [_format_cell(person, col) for col in columns]
        fh.write("| " + " | ".join(row) + " |\n")
    fh.write("\n")


def _format_cell(  # pylint: disable=too-many-return-statements
    person: Person, column: str
) -> str:
    if column == "Name":
        return person.canonical_name.replace("|", "\\|")
    if column == "Emails":
        return ", ".join(sorted(person.emails)).replace("|", "\\|")
    if column == "GitHub":
        return ", ".join(sorted(person.github_logins)).replace("|", "\\|")
    if column == "Msgs":
        return str(person.message_count)
    if column == "Issues":
        return str(person.issue_count)
    if column == "First":
        return person.first_seen.strftime("%Y-%m-%d") if person.first_seen else ""
    if column == "Last":
        return person.last_seen.strftime("%Y-%m-%d") if person.last_seen else ""
    if column == "Roles":
        return ", ".join(sorted(person.roles)).replace("|", "\\|")
    return ""
