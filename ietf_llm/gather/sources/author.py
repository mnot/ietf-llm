"""Resolve an author and list the Internet-Drafts they've written.

For a "follow this person" corpus: map a name to a Datatracker person,
then pull every draft they're an author of (via the documentauthor
through-table — the document endpoint's `authors__person` filter is
silently ignored).

This module resolves the person and lists their drafts. Driven from the
same resolved person id: the reviews they *wrote* (`sources.reviews`)
and their own list mail (`sources.author_lists` picks the lists,
`sources.author_mail` searches them by sender).

The archive has no cross-list by-sender search — IMAP `SEARCH FROM` is
per-folder — so the lists can't be discovered from the sender. They are
discovered from Datatracker instead: the groups the person holds a role
in and the groups owning their drafts. Add `--mailing-list <list>` for
one that neither turns up (it is then gathered in full, not by sender).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ...log import LogLevel, Verbosity, log
from .datatracker import _get_json

_PERSON_API = "https://datatracker.ietf.org/api/v1/person/person/"
_EMAIL_API = "https://datatracker.ietf.org/api/v1/person/email/"
_DOCAUTHOR_API = "https://datatracker.ietf.org/api/v1/doc/documentauthor/"


def resolve_person(
    spec: str, verbose: Verbosity = Verbosity.STATUS
) -> Optional[Tuple[int, str]]:
    """Resolve an author spec to `(person_id, canonical_name)`, or None.

    The spec can be (auto-detected, most-specific first):
      - an **email** (`mnot@mnot.net`) — unambiguous, the recommended
        form;
      - a **Datatracker person id** (`103881`) — unambiguous;
      - a **name** (`Mark Nottingham`) — convenient but ambiguous, so an
        exact (case-insensitive) match is required when there's more
        than one candidate.
    """
    spec = spec.strip()
    if "@" in spec:
        return _resolve_email(spec, verbose)
    if spec.isdigit():
        return _resolve_id(int(spec), verbose)
    return _resolve_name(spec, verbose)


def _resolve_email(email: str, verbose: Verbosity) -> Optional[Tuple[int, str]]:
    body = _get_json(f"{_EMAIL_API}{email}/")
    person_uri = (body or {}).get("person")
    if not person_uri:
        log(
            f"No Datatracker person owns the email '{email}'.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    tail = str(person_uri).rstrip("/").rsplit("/", 1)[-1]
    try:
        person_id = int(tail)
    except ValueError:
        log(
            f"Datatracker person URI for '{email}' has no numeric id: "
            f"{person_uri!r}.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    person = _get_json(str(person_uri)) or {}
    return (person_id, str(person.get("name") or email))


def _resolve_id(person_id: int, verbose: Verbosity) -> Optional[Tuple[int, str]]:
    body = _get_json(f"{_PERSON_API}{person_id}/")
    name = (body or {}).get("name")
    if not name:
        log(
            f"No Datatracker person with id {person_id}.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    return (person_id, str(name))


def _resolve_name(name: str, verbose: Verbosity) -> Optional[Tuple[int, str]]:
    body = _get_json(f"{_PERSON_API}?name__icontains={name}&limit=20")
    objects = (body or {}).get("objects") or []
    matches = [
        (int(o["id"]), str(o.get("name") or ""))
        for o in objects
        if o.get("id") is not None
    ]
    if not matches:
        log(
            f"No Datatracker person matches '{name}'.",
            verbose,
            level=LogLevel.ERROR,
        )
        return None
    exact = [m for m in matches if m[1].lower() == name.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    log(
        f"'{name}' is ambiguous on Datatracker — matches: "
        + ", ".join(sorted(n for _id, n in matches))
        + ". Re-run with the exact full name, an email, or the person id.",
        verbose,
        level=LogLevel.ERROR,
    )
    return None


def fetch_author_draft_names(
    person_id: int, verbose: Verbosity = Verbosity.STATUS
) -> List[str]:
    """Names of every Internet-Draft authored by `person_id`."""
    path: Optional[str] = f"{_DOCAUTHOR_API}?person={person_id}&limit=200"
    seen: set[str] = set()
    names: List[str] = []
    while path:
        body = _get_json(path)
        if not body:
            break
        for obj in body.get("objects") or []:
            doc = str(obj.get("document") or "").rstrip("/")
            name = doc.rsplit("/", 1)[-1] if doc else ""
            if name.startswith("draft-") and name not in seen:
                seen.add(name)
                names.append(name)
        path = (body.get("meta") or {}).get("next") or None
    log(
        f"  {len(names)} authored draft(s).",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return names
