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
  - Datatracker person id: one human posting under unrelated addresses /
    name spellings is consolidated via Datatracker's curated email→person
    table — exact-match, collision-free. See `people_linking.py`.
  - GitHub login → person, in three passes of decreasing precision
    (see `people_linking.py` for the orchestration):
      1. login ↔ email local-part: a login that equals the local-part of
         one of a Person's emails (`mnot` ↔ `mnot@mnot.net`).
      2. Datatracker `github_username` profile resource → the person's
         verified emails: authoritative (self-reported) and matched by
         exact email, so collision-free. Partial coverage.
      3. GitHub users API real name → an exact mailing-list display-name
         match: widest reach but name-only, so a last resort. We still
         decline fuzzy / first-name-only matching — it manufactures false
         merges (two different "Eric"s) far more often than real links.
    A GitHub-only actor that none of these link to is at least relabelled
    from its bare login to the resolved real name where we have one.

What we don't do (yet):

  - Cross-WG dedup. The registry is per-WG.
  - User-provided overrides (manual "these two are the same"). The
    registry is recomputed from cache each gather; no persistence yet.
"""

from __future__ import annotations

import email.policy
import email.utils
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from ..gather.sources.datatracker import fetch_wg_roles
from ..gather.sources.draft_authors import latest_draft_paths, parse_authors
from ..gather.sources.github import iter_issue_archives
from ..gather.sources.postal import locality_key, parse_document_year
from ..log import LogLevel, Verbosity, log
from ..paths import get_cache_dir, get_wg_file_cache_dir
from ..text import _parse_date
from .affiliation import INLINE_CAP, capped, group_affiliations, overflow_note
from .digest import _format_affiliations, write_people_digest
from .linking import (
    reconcile_mail_via_datatracker,
    resolve_github_user_names,
    resolve_github_via_datatracker,
)
from .meetings import ingest_meeting_participation

# `write_people_digest` (and the `_format_affiliations` helper some tests
# reach for) moved to `.digest`; re-exported here so `ietf_llm.people.<name>`
# keeps working. Listed in `__all__` so the re-export doesn't read as unused.
__all__ = [
    "Person",
    "Registry",
    "build_registry",
    "write_people_digest",
    "ingest_meeting_participation",
    "_format_affiliations",
]

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
    # Affiliations gathered from multiple sources, keyed by source tag:
    #   "draft:<doc-id>" — Authors' Addresses block of a draft/RFC the
    #       person authored; most authoritative (author-curated,
    #       chair-reviewed, per-document).
    #   "github" — GitHub `company` field, self-reported; corroborating
    #       second signal, sometimes stale. (future: datatracker, signature)
    # People legitimately ship different affiliations across drafts (job
    # change mid-WG, "Independent" on one and an employer on another) —
    # per-source keying preserves that; the renderer aggregates distinct
    # *values* with their sources ("Cloudflare (draft, github)"), so
    # cross-source agreement is itself signal. Empty when undocumented.
    affiliations: Dict[str, str] = field(default_factory=dict)
    # Publication year of the document behind each `affiliations` key,
    # where we know it. Only used to order the affiliations by recency —
    # the current employer is what a reader wants first — so a gap costs
    # ordering, not correctness. GitHub's `company` field is undated and
    # simply absent here.
    affiliation_years: Dict[str, int] = field(default_factory=dict)
    # Normalised place names seen *below* the organisation line in this
    # person's author blocks (see `note_address_lines`). An author who
    # states no organisation puts their city in the organisation's slot,
    # which no per-line rule can tell from a one-word employer; matching
    # against the same person's other documents can.
    localities: Set[str] = field(default_factory=set)
    # Domains of every email seen. NOT the same as affiliation
    # (participants often use personal email, and some hold several
    # affiliations); surfaced only on explicit request as a fallback
    # signal, not an attribution.
    email_domains: Set[str] = field(default_factory=set)
    # Meeting participation, attached link-only by the meetings pass (see
    # `people/meetings.py`): these signals only ever land on a person already
    # known from mail / drafts / GitHub / roles — a meeting never mints a new
    # actor. `attended_sessions` is id-linked (Datatracker `attended` API,
    # collision-free); `spoke_at_meetings` is name-linked (transcript speaker
    # labels, exact canonical-name match). Both hold meeting codes (`ietf125`,
    # `interim20260401`).
    attended_sessions: Set[str] = field(default_factory=set)
    spoke_at_meetings: Set[str] = field(default_factory=set)
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


_DMARC_RE = re.compile(r"^([^@\s]+)=40([^@\s]+)@dmarc\.ietf\.org$", re.IGNORECASE)

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

#: Editor/Author tags name up to this many documents, then show a count.
_ROLE_DOC_CAP = 2


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
            if (
                _looks_like_email(person.canonical_name)
                or person.canonical_name == addr
            ):
                person.canonical_name = name
        elif name and _looks_like_email(person.canonical_name):
            person.canonical_name = name

        if addr:
            person.emails.add(addr)
            self._by_email[addr] = person
            if "@" in addr:
                person.email_domains.add(addr.rsplit("@", 1)[-1])
        if name:
            self._by_name[name.lower()] = person
        if raw_from:
            person.raw_from_headers.add(raw_from)
        person.message_count += 1
        person.touch_date(date)
        return person

    def add_github_company(self, login: str, company: Optional[str]) -> None:
        """Record a GitHub user's self-reported `company` field as an
        affiliation hint. The login must already be linked to a Person
        (call after `add_github_author`); a no-op otherwise.

        Stored under the `"github"` source tag so the renderer can
        report it with provenance. Empty / None values are silently
        skipped — GitHub users frequently leave `company` blank.
        """
        if not login or not company:
            return
        person = self._by_github.get(login.strip())
        if person is None:
            return
        cleaned = company.strip().lstrip("@")
        if cleaned:
            person.affiliations["github"] = cleaned

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

    def link_github_identity(
        self,
        login: str,
        name: Optional[str],
        emails: Iterable[str],
    ) -> str:
        """Link a GitHub-only actor to a mailing-list actor using identity
        facts resolved from an external source (Datatracker profile, or
        the GitHub users API).

        `login` must already be in the registry as a GitHub author. We try
        to merge its Person into a mailing-list Person, matching first on
        any of `emails` (exact, collision-free — email is the registry's
        primary key) and then on exact `name` (case-insensitive). On a
        match the two Persons are merged and the result is "linked".

        With no mailing-list match we don't invent one: we only upgrade the
        bare login to the real `name` so the people file shows a human
        rather than `kmadhavan-msft` ("named"). We deliberately do NOT
        attach the resolved emails to an unlinked actor — those addresses
        never appeared on the list, and adding them would miscount a
        GitHub-only participant as active on both surfaces. Returns
        "linked", "named", or "unmatched".
        """
        login = (login or "").strip()
        gh_person = self._by_github.get(login)
        if gh_person is None:
            return "unmatched"
        # Already linked to a mailing-list identity (by the local-part
        # heuristic at ingest, or an earlier call): nothing to do.
        if gh_person.emails or gh_person.message_count > 0:
            return "already"

        target = self._find_mail_person(name, emails, exclude=gh_person)
        if target is not None:
            self._merge_persons(target, gh_person)
            return "linked"

        if name and gh_person.canonical_name in gh_person.github_logins:
            gh_person.canonical_name = name
            self._by_name[name.lower()] = gh_person
            return "named"
        return "unmatched"

    def _find_mail_person(
        self,
        name: Optional[str],
        emails: Iterable[str],
        exclude: Person,
    ) -> Optional[Person]:
        """Find an existing mailing-list Person matching any of `emails`
        (exact) or `name` (exact, case-insensitive). Returns None when no
        distinct match exists. Name matches must carry mail signal so we
        don't link onto a draft-author / role-only stub."""
        for raw in emails or []:
            norm = _normalise_email(raw)
            if not norm:
                continue
            cand = self._by_email.get(norm)
            if cand is not None and cand is not exclude:
                return cand
        if name:
            cand = self._by_name.get(name.lower())
            if (
                cand is not None
                and cand is not exclude
                and (cand.emails or cand.message_count > 0)
            ):
                return cand
        return None

    def merge_into(self, keep: Person, drop: Person) -> None:
        """Public entry point to fold `drop`'s identity into `keep`. Used by the
        mail-reconciliation pass in `people_linking.py`; see `_merge_persons`."""
        self._merge_persons(keep, drop)

    def _merge_persons(self, keep: Person, drop: Person) -> None:
        """Fold `drop` into `keep` and remove it from the registry.

        `keep` is the surviving identity (the mailing-list Person, which
        carries the message history and a reviewed name); `drop` is the
        GitHub-only Person being linked in. Every surface form and counter
        moves across, and each index entry pointing at `drop` is repointed
        at `keep`.
        """
        if keep is drop:
            return
        # Decide the canonical name before merging logins, so a freshly
        # absorbed login can't make keep's existing name look "weak".
        keep_name_weak = (
            _looks_like_email(keep.canonical_name)
            or keep.canonical_name in keep.github_logins
        )
        drop_name_real = bool(drop.canonical_name) and (
            drop.canonical_name not in drop.github_logins
        )
        if keep_name_weak and drop_name_real:
            keep.canonical_name = drop.canonical_name

        keep.emails |= drop.emails
        keep.github_logins |= drop.github_logins
        keep.raw_from_headers |= drop.raw_from_headers
        keep.roles |= drop.roles
        keep.authored_documents |= drop.authored_documents
        keep.edited_documents |= drop.edited_documents
        keep.email_domains |= drop.email_domains
        keep.attended_sessions |= drop.attended_sessions
        keep.spoke_at_meetings |= drop.spoke_at_meetings
        keep.localities |= drop.localities
        # Affiliations: keep wins on key clashes; drop fills gaps.
        for source, org in drop.affiliations.items():
            keep.affiliations.setdefault(source, org)
        for source, year in drop.affiliation_years.items():
            keep.affiliation_years.setdefault(source, year)
        keep.message_count += drop.message_count
        keep.issue_count += drop.issue_count
        keep.touch_date(drop.first_seen)
        keep.touch_date(drop.last_seen)

        for email_addr in drop.emails:
            self._by_email[email_addr] = keep
        for gh_login in drop.github_logins:
            self._by_github[gh_login] = keep
        if drop.canonical_name:
            self._by_name[drop.canonical_name.lower()] = keep
        self._by_name[keep.canonical_name.lower()] = keep
        self.persons = [person for person in self.persons if person is not drop]

    def add_document_author(
        self,
        name: str,
        email_address: Optional[str],
        document: str,
        is_editor: bool = False,
        organization: Optional[str] = None,
        year: Optional[int] = None,
        address_lines: Optional[Iterable[str]] = None,
    ) -> Optional[Person]:
        """Record `name` as an author of `document` (a draft/RFC basename).

        Merges by email then by name with the existing registry, same
        way add_datatracker_role does. The document basename is stored
        without the version suffix (`draft-ietf-aipref-vocab` not
        `…-06`) so multiple versions don't multiply the role count.

        `year` is the document's publication year, used only to order
        affiliations by recency. `address_lines` are the block's postal
        lines below the organisation slot — corroboration that lets a
        later pass tell a city apart from a one-word employer.
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
        if organization:
            stripped = organization.strip()
            if stripped:
                person.affiliations[f"draft:{document}"] = stripped
                if year is not None:
                    person.affiliation_years[f"draft:{document}"] = year
        for line in address_lines or ():
            key = locality_key(line)
            if key:
                person.localities.add(key)
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

    def person_for_name(self, canonical_name: str) -> Optional[Person]:
        """Look up a Person by their canonical name. Used by file
        renderers that have already canonicalised a from-header or
        GitHub login and want the role-bearing Person back."""
        if not canonical_name:
            return None
        return self._by_name.get(canonical_name.lower())

    def person_for_email(self, address: str) -> Optional[Person]:
        """Look up a Person by an (already-normalised) email address. Used by
        the attendance pass to resolve a Datatracker person id back to the
        registry via that person's known addresses."""
        if not address:
            return None
        return self._by_email.get(address.strip().lower())

    def affiliation_tag(self, canonical_name: str) -> Optional[str]:
        """Compact affiliation rendering for inline use (participants
        line). Returns None when no source has produced an affiliation
        — silence beats guessing.

        Near-duplicate spellings collapse ("Akamai Technologies, Inc."
        into "Akamai"), the survivors are ordered most-recent-first, and
        at most `INLINE_CAP` render — a prolific author's whole career of
        employers is real history but makes the line unreadable, so the
        rest become a count. Source provenance is NOT inlined either;
        `_format_affiliations` is the rendering that shows it.

        Cases:
          - No sources known → None
          - One distinct value → "Cloudflare"
          - A few → "Cloudflare; Fastly"
          - Many → "Cloudflare; Fastly; Akamai (+4 earlier)"
        """
        person = self.person_for_name(canonical_name)
        if person is None or not person.affiliations:
            return None
        shown, dropped = capped(group_affiliations(person), INLINE_CAP)
        if not shown:
            return None
        return "; ".join(g.display for g in shown) + overflow_note(dropped)

    def role_tag(self, canonical_name: str) -> Optional[str]:
        """All relevant role tags for inline use, "/"-joined.

        Order: leadership (Chair > AD > Tech Advisor > Secretary), then
        editorship, then authorship — ALL applicable, joined with `/`.
        Multi-hat status is load-bearing (a Chair who also authored the
        draft under discussion has a procedural conflict worth surfacing).
        Editor/Author name their document(s) — a bare "Author" misleads in
        a multi-draft group — up to `_ROLE_DOC_CAP`, then a count ("Author
        of 5 drafts"): a long list neither disambiguates nor fits inline.

        Returns None when the person carries no role we surface.
        """
        person = self.person_for_name(canonical_name)
        if person is None:
            return None
        bits: List[str] = []
        for label in ("Chair", "Area Director", "Tech Advisor", "Secretary"):
            if label in person.roles:
                bits.append({"Area Director": "AD"}.get(label, label))
        for role, docs in (
            ("Editor", person.edited_documents),
            ("Author", person.authored_documents),
        ):
            if not docs:
                continue
            names = sorted(docs)
            listed = ", ".join(names)
            bits.append(
                f"{role} of {listed}"
                if len(names) <= _ROLE_DOC_CAP
                else f"{role} of {len(names)} drafts"
            )
        if not bits:
            return None
        return "/".join(bits)

    # ----- iteration -------------------------------------------------------

    def all_persons(self) -> List[Person]:
        return list(self.persons)

    def leadership(self) -> List[Person]:
        """Persons holding any formal WG role, sorted by highest role then name.

        A person with several roles (e.g. Chair *and* Area Director) ranks by
        their most senior one — the minimum order value — not whichever role
        happens to sort first alphabetically.
        """
        return sorted(
            (p for p in self.persons if p.roles),
            key=lambda p: (
                min(_LEADERSHIP_ROLE_ORDER.get(r, 99) for r in p.roles),
                p.canonical_name,
            ),
        )


def _looks_like_email(name: str) -> bool:
    return "@" in name


# --- Builders --------------------------------------------------------------


def build_registry(
    wg: str,
    verbose: Verbosity = Verbosity.STATUS,
    with_datatracker_roles: bool = True,
) -> Registry:
    """Build a Registry by scanning IMAP, GitHub, Datatracker, and drafts.

    The Datatracker step is best-effort: if the network call fails or
    the WG isn't recognised, roles are silently omitted and the rest
    of the registry stands. Pass `with_datatracker_roles=False` to
    skip it entirely (used for synthetic / `x-` corpora that have no
    WG record at Datatracker).
    """
    registry = Registry()
    _ingest_mail(wg, registry, verbose)
    # Consolidate mailing-list identities that Datatracker maps to one person
    # id (the same human under unrelated addresses / name spellings). Runs
    # before the GitHub ingest/passes so they inherit the merged identities.
    reconcile_mail_via_datatracker(registry, verbose)
    _ingest_github(wg, registry, verbose)
    # Link still-unlinked GitHub authors to mailing-list identities, in
    # order of precision. Both run before roles/drafts so name/email
    # matches only ever land on a mailing-list Person.
    #
    # 1. Datatracker `github_username` profile resources — authoritative
    #    (the person claimed the login themselves) and matched by verified
    #    email, so collision-free. Partial coverage.
    resolve_github_via_datatracker(registry, verbose)
    # 2. GitHub users API real name — wider reach, matched by display name
    #    only, so a last resort. Also upgrades a bare login to a real name
    #    and records the self-reported `company` affiliation.
    resolve_github_user_names(registry, verbose)
    if with_datatracker_roles:
        _ingest_datatracker_roles(wg, registry, verbose)
    _ingest_draft_authors(wg, registry, verbose)
    # Meeting participation — attendance (id-linked) and transcript speaking
    # (name-linked). Runs LAST so it matches against the fully-assembled
    # registry, and link-only so it never adds a meeting-only actor.
    ingest_meeting_participation(registry, wg, verbose)
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
                    msg = email.message_from_binary_file(
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
    """Read each `github/<repo-slug>.json` archive and feed issue authors."""
    cache_dir = os.path.join(get_cache_dir(), wg, "files")
    archives_dir = os.path.join(cache_dir, "github")
    count = 0
    for data in iter_issue_archives(archives_dir):
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


def _ingest_datatracker_roles(wg: str, registry: Registry, verbose: Verbosity) -> None:
    """Fetch chairs/ADs/advisors from Datatracker and add to the registry."""
    for role in fetch_wg_roles(wg, verbose=verbose):
        registry.add_datatracker_role(role.name, role.email, role.label)


def _ingest_draft_authors(wg: str, registry: Registry, verbose: Verbosity) -> None:
    """Parse the Authors' Addresses section of each draft / RFC in the cache.

    Stable name spellings (taken from the document front-matter that
    the chairs review) override any earlier mailing-list-derived form.
    """
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
        year = parse_document_year(text)
        for author in parse_authors(text):
            registry.add_document_author(
                author.name,
                author.email,
                document=doc_id,
                is_editor=author.is_editor,
                organization=author.organization,
                year=year,
                address_lines=author.address_lines,
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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A zone-less timestamp parses tz-naive; force UTC so a later comparison
    # against the tz-aware mail-derived dates can't raise TypeError and abort
    # the GitHub ingest pass.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
