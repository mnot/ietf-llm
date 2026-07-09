"""Render the `<wg>-_people.md` digest from a built `Registry`.

Split out of `people.py` (which owns the registry and its identity passes)
purely to hold that module under its line budget; `write_people_digest` and
its table helpers drive `Registry` / `Person` through their public surface.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set

from ..atomicio import atomic_open
from ..log import LogLevel, Verbosity, log
from ..paths import digest_path, remove_stale_digest

if TYPE_CHECKING:
    from . import Person, Registry


def write_people_digest(
    wg: str,
    cache_dir: str,
    registry: "Registry",
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
        remove_stale_digest(cache_dir, "people")
        return None

    linked, mail_only, gh_only = _bucket_persons(persons)

    out_path = digest_path(cache_dir, "people")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    leaders = registry.leadership()
    with atomic_open(out_path) as fh:
        fh.write(f"# {wg}: participants\n\n")
        fh.write(
            f"_{len(persons)} distinct actors. Multiple surface forms "
            "(addresses, DMARC-rewritten variants, GitHub logins) for the "
            "same person are consolidated — `Top senders` columns and "
            "issue authors elsewhere in the corpus use these canonical "
            "names._\n\n"
        )
        fh.write(
            "_**Affiliation** is gathered from two sources, with "
            "provenance shown in each cell: `(draft)` = the "
            "**Authors' Addresses** block of a draft the person has "
            "authored — the most authoritative source. `(github)` = "
            "their self-reported GitHub `company` field — weaker, "
            "but a useful corroborating signal when both sources name "
            "the same org (`Cloudflare (draft, github)`).\n\n"
            "Affiliation is implementer signal — load-bearing for "
            "'rough consensus and running code'. It is NOT a license "
            "to claim someone speaks for the organisation: people "
            "participate as individuals. Aggregate, don't attribute.\n\n"
            "Blank = no documented signal from either source. Do NOT "
            "infer affiliation from email domain — participants often "
            "use personal email, and some hold multiple affiliations, "
            "representing some or none of those interests in a given "
            "discussion._\n\n"
        )
        fh.write(
            "_**Meetings** counts this person's meeting participation, "
            "link-only: `N att` = sessions the Datatracker attendance record "
            "places them at (id-matched, so exact); `N spoke` = meetings a "
            "transcript captures them speaking at (name-matched). Attendance "
            "is presence, NOT a position — do not read it as support or "
            "opposition. The full per-meeting rosters (including attendees "
            "who are not registry participants) live in each meeting's "
            "`attendance.md`._\n\n"
        )

        # Formal WG leadership comes from Datatracker. Surfaced first
        # because "who runs this WG" is usually what the reader wants
        # to know before scrolling through 100+ participants.
        if leaders:
            fh.write(f"## Working Group leadership ({len(leaders)})\n\n")
            fh.write("| Role | Name | Affiliation | Email |\n|---|---|---|---|\n")
            for person in leaders:
                roles_text = ", ".join(sorted(person.roles))
                primary_email = next(iter(sorted(person.emails)), "")
                aff = _format_affiliations(person)
                fh.write(
                    f"| {roles_text} | {person.canonical_name} | "
                    f"{aff} | {primary_email} |\n"
                )
            fh.write("\n")

        # Document authors — surfaced next because "who wrote this
        # draft" is the second question after "who runs the WG".
        authors = [p for p in persons if p.authored_documents or p.edited_documents]
        if authors:
            fh.write(f"## Document authors / editors ({len(authors)})\n\n")
            fh.write("| Name | Documents | Affiliation | Email |\n|---|---|---|---|\n")
            authors.sort(key=lambda p: p.canonical_name.lower())
            for person in authors:
                primary_email = next(iter(sorted(person.emails)), "")
                parts = sorted(person.authored_documents) + [
                    f"{d} (ed.)" for d in sorted(person.edited_documents)
                ]
                docs = ", ".join(parts)
                aff = _format_affiliations(person)
                fh.write(
                    f"| {person.canonical_name} | {docs} | "
                    f"{aff} | {primary_email} |\n"
                )
            fh.write("\n")

        if linked:
            fh.write(f"## Active on both mailing list and GitHub ({len(linked)})\n\n")
            _write_actor_table(
                fh,
                linked,
                columns=(
                    "Name",
                    "Roles",
                    "Affiliation",
                    "Emails",
                    "GitHub",
                    "Msgs",
                    "Issues",
                    "Meetings",
                ),
            )
        if mail_only:
            fh.write(f"## Mailing list only ({len(mail_only)})\n\n")
            _write_actor_table(
                fh,
                mail_only,
                columns=(
                    "Name",
                    "Roles",
                    "Affiliation",
                    "Emails",
                    "Msgs",
                    "Meetings",
                    "First",
                    "Last",
                ),
            )
        if gh_only:
            fh.write(f"## GitHub only ({len(gh_only)})\n\n")
            _write_actor_table(
                fh,
                gh_only,
                columns=("Name", "GitHub", "Issues"),
            )

    log(
        f"Wrote people digest: {len(linked)} linked, "
        f"{len(mail_only)} mail-only, {len(gh_only)} github-only",
        verbose,
        level=LogLevel.STATUS,
    )
    return out_path


def _format_affiliations(person: "Person") -> str:
    """Render a Person's affiliations for a digest table cell, with
    source provenance.

    Each distinct organisation value renders as `Org (sources)` where
    `sources` is a "/"-joined list of source kinds — `draft`, `github`,
    etc. — sorted with `draft` first (most authoritative). When the
    same org is corroborated by multiple sources, the cell shows that
    explicitly: `Cloudflare (draft, github)` is stronger signal than
    `Cloudflare (github)` alone, and the renderer surfaces it.

    Returns "" (empty cell, not "—") when no source has produced an
    affiliation — honest blank beats a default.
    """
    if not person.affiliations:
        return ""
    # Aggregate distinct orgs → set of source kinds. Source kinds
    # come from the part of the key before the first ":" — so every
    # "draft:..." key collapses to "draft", every "github" stays.
    org_sources: Dict[str, Set[str]] = {}
    for source_key, org in person.affiliations.items():
        if not org:
            continue
        kind = source_key.split(":", 1)[0]
        org_sources.setdefault(org, set()).add(kind)
    # Order: most-sourced first (signal), then alphabetical.
    source_order = {"draft": 0, "datatracker": 1, "github": 2, "signature": 3}
    ranked = sorted(
        org_sources.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    bits: List[str] = []
    for org, sources in ranked:
        ordered_sources = sorted(sources, key=lambda s: (source_order.get(s, 99), s))
        bits.append(f"{org} ({', '.join(ordered_sources)})")
    return "; ".join(bits).replace("|", "\\|")


def _bucket_persons(
    persons: "Iterable[Person]",
) -> "tuple[List[Person], List[Person], List[Person]]":
    linked: "List[Person]" = []
    mail_only: "List[Person]" = []
    gh_only: "List[Person]" = []
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
    fh: Any, persons: "List[Person]", columns: Iterable[str]
) -> None:
    columns = list(columns)
    fh.write("| " + " | ".join(columns) + " |\n")
    fh.write("|" + "|".join("---" for _ in columns) + "|\n")
    for person in persons:
        row = [_format_cell(person, col) for col in columns]
        fh.write("| " + " | ".join(row) + " |\n")
    fh.write("\n")


def _format_cell(  # pylint: disable=too-many-return-statements
    person: "Person", column: str
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
    if column == "Affiliation":
        return _format_affiliations(person)
    if column == "Email domain":
        return ", ".join(sorted(person.email_domains)).replace("|", "\\|")
    if column == "Meetings":
        bits: List[str] = []
        if person.attended_sessions:
            bits.append(f"{len(person.attended_sessions)} att")
        if person.spoke_at_meetings:
            bits.append(f"{len(person.spoke_at_meetings)} spoke")
        return ", ".join(bits)
    return ""
