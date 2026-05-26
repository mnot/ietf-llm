"""Thin client for the IETF Datatracker JSON API.

We only use the small slice we need: chairs / ADs / advisors / etc.
for a given WG. The Datatracker exposes other endpoints (drafts,
documents, ballots, …) which could be added here later in the same
shape.

Why a real API client and not HTML scraping the WG about page:
the about page's table layout has changed before; the JSON API has
been stable for years and returns canonical IDs we can correlate
with the Registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from ..utils import DEFAULT_HEADERS, LogLevel, Verbosity, log

_API_BASE = "https://datatracker.ietf.org/api/v1"

# Map Datatracker role slugs to a friendly display label. Anything not
# in this map is included verbatim with a leading uppercase letter.
_ROLE_LABELS = {
    "chair": "Chair",
    "ad": "Area Director",
    "techadv": "Tech Advisor",
    "secr": "Secretary",
    "auth": "Author",
    "editor": "Editor",
    "delegate": "Delegate",
}

#: Roles we surface prominently. Everything else collects under "Other".
_LEADERSHIP_ROLES = {"chair", "ad", "techadv", "secr"}


@dataclass
class Role:
    """One role assignment for one person on a WG."""

    role: str  # raw Datatracker slug (e.g. "chair", "ad")
    label: str  # human label (e.g. "Chair", "Area Director")
    name: str  # full display name from Datatracker
    email: Optional[str]  # mailbox if extractable


def label_for(role_slug: str) -> str:
    """Human-readable label for a Datatracker role slug."""
    return _ROLE_LABELS.get(role_slug, role_slug.capitalize())


def is_leadership(role_slug: str) -> bool:
    return role_slug in _LEADERSHIP_ROLES


# --- API client ------------------------------------------------------------


_EMAIL_FROM_URL = re.compile(r"/api/v1/person/email/([^/]+)/?$")
_ROLE_NAME_FROM_URL = re.compile(r"/api/v1/name/rolename/([^/]+)/?$")


def _get_json(path_or_url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """GET a Datatracker JSON endpoint. Returns the decoded body or None."""
    url = (
        path_or_url
        if path_or_url.startswith("http")
        else f"https://datatracker.ietf.org{path_or_url}"
    )
    if "?" in url:
        url = url + "&format=json"
    else:
        url = url + "?format=json"
    try:
        response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, dict) else None
    except (requests.RequestException, ValueError):
        return None


def fetch_wg_roles(
    wg: str, verbose: Verbosity = Verbosity.STATUS
) -> List[Role]:
    """Return the role assignments for a WG (chairs, ADs, advisors, …).

    Returns an empty list if Datatracker is unreachable or the WG
    isn't recognised. Best-effort: a missing person dereference yields
    a Role with the raw email local-part as the name; we never block
    the gather pipeline on this.
    """
    roles_body = _get_json(
        f"{_API_BASE}/group/role/?group__acronym={wg}"
    )
    if not roles_body or "objects" not in roles_body:
        log(
            f"Could not fetch Datatracker roles for {wg}; skipping.",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return []

    out: List[Role] = []
    # Tiny per-call cache so two chairs of the same WG don't re-fetch
    # the same person endpoint (they don't, but the pattern is cheap).
    person_cache: Dict[str, str] = {}

    for obj in roles_body["objects"]:
        role_url = obj.get("name", "")
        role_match = _ROLE_NAME_FROM_URL.search(role_url)
        if not role_match:
            continue
        role_slug = role_match.group(1)

        email_url = obj.get("email", "")
        email_match = _EMAIL_FROM_URL.search(email_url)
        email = email_match.group(1) if email_match else None

        person_url = obj.get("person", "")
        if person_url in person_cache:
            name = person_cache[person_url]
        else:
            person_body = _get_json(person_url)
            name = ""
            if person_body:
                name = (
                    person_body.get("name")
                    or person_body.get("ascii")
                    or ""
                )
            if not name and email:
                name = email.split("@", 1)[0]
            person_cache[person_url] = name

        out.append(
            Role(
                role=role_slug,
                label=label_for(role_slug),
                name=name,
                email=email,
            )
        )

    log(
        f"Datatracker roles for {wg}: "
        + ", ".join(f"{r.label} {r.name}" for r in out),
        verbose,
        level=LogLevel.STATUS,
    )
    return out
