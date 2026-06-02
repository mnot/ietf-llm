"""GitHub-identity linking passes for the people Registry.

`people.py` builds the registry and owns the merge/link primitives on
`Registry`; this module owns the *orchestration* of the two
identity-resolution passes that attach GitHub authors to mailing-list
identities, in order of precision:

1. `resolve_github_via_datatracker` — self-reported `github_username`
   profile resources on Datatracker, matched by verified email. Exact,
   collision-free, but partial coverage.
2. `resolve_github_user_names` — the GitHub users API real name, matched
   by display name. Wider reach, name-only, so a last resort. Also
   upgrades bare logins to real names and records `company` affiliation.

Kept out of `people.py` purely to hold that module under its line
budget; both functions drive `Registry` through its public methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from .gather.datatracker_github import resolve_via_datatracker
from .gather.github_users import resolve_logins
from .utils import LogLevel, Verbosity, log

if TYPE_CHECKING:
    from .people import Person, Registry


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
