"""Identity-resolution passes for the people Registry.

`people.py` builds the registry and owns the merge/link primitives on
`Registry`; this module owns the *orchestration* of the passes that
consolidate identities, run from `build_registry`:

- `reconcile_mail_via_datatracker` — consolidate mailing-list addresses
  that Datatracker maps to one person id (the mail-side identity spine).
  Runs first, before any GitHub ingest, so the GitHub passes inherit the
  consolidated mail identities.

Then, attaching GitHub authors to mailing-list identities in order of
precision:

1. `resolve_github_via_datatracker` — self-reported `github_username`
   profile resources on Datatracker, matched by verified email. Exact,
   collision-free, but partial coverage.
2. `resolve_github_user_names` — the GitHub users API real name, matched
   by display name. Wider reach, name-only, so a last resort. Also
   upgrades bare logins to real names and records `company` affiliation.

Kept out of `people.py` purely to hold that module under its line
budget; every function drives `Registry` through its public methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, List

from .gather.datatracker_github import resolve_via_datatracker
from .gather.datatracker_people import resolve_addresses
from .gather.github_users import resolve_logins
from .utils import LogLevel, Verbosity, log

if TYPE_CHECKING:
    from .people import Person, Registry


def _consolidate_groups(
    registry: "Registry",
    groups: "Iterable[List[Person]]",
) -> int:
    """Merge each group of Persons that resolved to one Datatracker person id.

    The survivor is the most-active member (messages + issues), tie-broken by
    canonical name, so the more-established display name wins. Canonical-name
    handling stays merge-only — `merge_into` adopts the absorbed name only when
    the survivor's is still email-ish — so recognised names don't churn.
    Returns the number of merges performed.
    """
    merged = 0
    for group in groups:
        members = sorted(
            group, key=lambda p: (-(p.message_count + p.issue_count), p.canonical_name)
        )
        for drop in members[1:]:
            registry.merge_into(members[0], drop)
            merged += 1
    return merged


def reconcile_mail_via_datatracker(
    registry: "Registry",
    verbose: Verbosity,
) -> None:
    """Consolidate mailing-list Persons that Datatracker maps to one person id.

    A single human often posts under unrelated addresses with different name
    spellings (`M. Nottingham <mnot@fastly.com>` vs `Mark Nottingham
    <mnot@mnot.net>`); the name / DMARC merge in `people.py` misses that.
    Datatracker records all of a person's addresses under one id, so resolving
    each registry address and grouping by that id closes the gap at exact-match
    precision. Runs before the GitHub passes so they inherit the consolidated
    identities. Best-effort: a Datatracker outage leaves the registry as-is.
    """
    # Address -> Person over the whole registry. These addresses are de-munged
    # and relay-dropped by `_normalise_email` at ingest, so role / noreply relay
    # addresses never reach the resolver.
    addr_to_person: "Dict[str, Person]" = {
        e: p for p in registry.persons for e in p.emails
    }
    if not addr_to_person:
        return
    resolved = resolve_addresses(sorted(addr_to_person), verbose=verbose)
    if not resolved:
        return
    # Group the registry Persons by the Datatracker person uri their addresses
    # resolve to; a uri spanning >=2 distinct Persons is one human under several
    # identities, so consolidate each such group.
    by_uri: "Dict[str, List[Person]]" = {}
    for addr, uri in resolved.items():
        person = addr_to_person.get(addr)
        if person is None:
            continue
        bucket = by_uri.setdefault(uri, [])
        if not any(p is person for p in bucket):
            bucket.append(person)
    n_merged = _consolidate_groups(registry, by_uri.values())
    if n_merged:
        log(
            f"  Datatracker person id: merged {n_merged} mail identity group(s)",
            verbose,
            level=LogLevel.STATUS,
        )


def _unlinked_github_logins(registry: "Registry") -> "Dict[str, Person]":
    """Map every GitHub login still unattached to a mailing-list identity
    to its (GitHub-only) Person. "Unlinked" means no mail signal — no
    email and no messages — i.e. the actor sits in the `github-only`
    bucket. Used by both linking passes to know what's left to resolve."""
    out: "Dict[str, Person]" = {}
    for person in registry.persons:
        if not person.github_logins:
            continue
        if person.emails or person.message_count > 0:
            continue
        for login in person.github_logins:
            out[login] = person
    return out


def resolve_github_via_datatracker(
    registry: "Registry",
    verbose: Verbosity,
) -> None:
    """Link unlinked GitHub authors to mailing-list identities using the
    `github_username` external resources people set on their Datatracker
    profiles.

    Authoritative and matched by verified email, so it carries no
    name-collision risk — the highest-precision linker we have. Coverage
    is partial (only a few hundred people across IETF have set the field),
    so it runs first and the GitHub-name pass mops up the rest. Where a
    login resolves to a real name but no mailing-list match, the bare
    login is still upgraded to that name.
    """
    candidates = _unlinked_github_logins(registry)
    if not candidates:
        return
    resolved = resolve_via_datatracker(list(candidates), verbose=verbose)
    if not resolved:
        return
    n_linked = 0
    n_named = 0
    for login, info in resolved.items():
        outcome = registry.link_github_identity(login, info.name, info.emails)
        if outcome == "linked":
            n_linked += 1
        elif outcome == "named":
            n_named += 1
    if n_linked or n_named:
        log(
            f"  Datatracker github_username: linked {n_linked}, "
            f"named {n_named} login(s)",
            verbose,
            level=LogLevel.STATUS,
        )


def resolve_github_user_names(
    registry: "Registry",
    verbose: Verbosity,
) -> None:
    """For each still-unlinked GitHub login, look up the user's real name
    via the GitHub users API and use it two ways:

    1. **Link** — if the real name exactly matches a mailing-list Person's
       name, merge the two (the actor was active on both surfaces but the
       email local-part / Datatracker passes didn't catch it). Name-only
       matching, so a last resort after the email-exact Datatracker pass.
    2. **Name** — otherwise upgrade the bare login to the real name so the
       people file shows a human, not `kmadhavan-msft`.

    Also captures the GitHub user's self-reported `company` field as an
    affiliation signal — stored under the "github" source tag, distinct
    from the draft-derived signal so the renderer can show agreement when
    both sources name the same org. Logins where the API returns no name
    (or 404) keep their current canonical name; the absence is cached so
    we don't re-ask next gather.
    """
    candidates = _unlinked_github_logins(registry)
    if not candidates:
        return

    log(
        f"Resolving {len(candidates)} GitHub login(s) to real names...",
        verbose,
        level=LogLevel.STATUS,
    )
    resolved = resolve_logins(list(candidates), verbose=verbose)
    n_linked = 0
    n_named = 0
    n_companies = 0
    for login, info in resolved.items():
        if info.name:
            # No emails from the GitHub API, so this links by name only.
            outcome = registry.link_github_identity(login, info.name, [])
            if outcome == "linked":
                n_linked += 1
            elif outcome == "named":
                n_named += 1
        # Record the company field as an affiliation source. We always
        # try this — even when name resolution didn't upgrade, an
        # already-named Person may still have a useful `company`.
        if info.company:
            registry.add_github_company(login, info.company)
            n_companies += 1
    if n_linked or n_named or n_companies:
        log(
            f"  GitHub names: linked {n_linked}, named {n_named} login(s); "
            f"{n_companies} affiliation(s) from `company`",
            verbose,
            level=LogLevel.STATUS,
        )
