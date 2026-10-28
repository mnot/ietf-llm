"""Name keys and role-account detection for the people Registry.

`name_key` is what `Registry._by_name` is keyed on: two display names that
differ only in case, diacritics, hyphenation, `Last, First` order or a
trailing parenthetical (`Deen, Glenn (Comcast Cable)`, `Eric Vyncke
(evyncke)`) share a key and so resolve to one Person. The key is for lookup
only. Where two spellings of one name meet, `cleaner_name` picks the one to
display: an undecorated spelling over a decorated one. Names that
Datatracker or a draft supplies are authoritative and overwrite the canonical
name outright, so the preference governs mail-derived spellings only.

`is_role_account` separates mailboxes and bots (`internet-drafts@ietf.org`,
`The IESG`, `Repository Activity Summary Bot`) from people, so the digest
can list them apart rather than as rows in a table of participants.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, List, Optional, Set

if TYPE_CHECKING:
    from . import Person

_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_SEPARATOR_RE = re.compile(r"[\s_\-‐-―]+")
_LOGIN_SEPARATOR_RE = re.compile(r"[_\-]+")
_LATIN_LIMIT = 0x250
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"}


def name_key(name: str) -> str:
    """Fold a display name to the key used for identity lookup.

    Case, Latin diacritics, hyphens and underscores are ignored, a single
    `Last, First` comma is reordered, and trailing `(…)` groups are dropped.
    A comma before a generational or academic suffix (`Smith, Jr.`) is left
    alone — that is not a surname/forename split. A name with nothing left
    to key on (`-`) keys as itself, so such names don't all collide on "".
    """
    text = _strip_latin_accents(name).strip()
    stripped, _ = _split_notes(text)
    # A name that is nothing but a parenthetical keeps it: better an odd key
    # than every such name colliding on "".
    text = _first_last((stripped or text).casefold())
    return _SEPARATOR_RE.sub(" ", text).strip() or text


def _split_notes(text: str) -> "tuple[str, List[str]]":
    """Split trailing `(…)` groups off a name, returning the rest and the
    groups, innermost-last-first (as written, not casefolded)."""
    notes: List[str] = []
    while True:
        match = _TRAILING_PAREN_RE.search(text)
        if match is None:
            return text, notes
        notes.append(match.group(0).strip(" ()"))
        text = text[: match.start()]


def without_parentheticals(name: str) -> str:
    """The name with its trailing `(…)` groups removed."""
    return _split_notes(name.strip())[0].strip()


def trailing_parentheticals(name: str) -> List[str]:
    """The trailing `(…)` groups of a display name, as written:
    `Deen, Glenn (Comcast Cable)` -> `["Comcast Cable"]`."""
    return _split_notes(name.strip())[1]


def _first_last(text: str) -> str:
    """Reorder a single `Last, First` comma; anything else is returned as is."""
    if text.count(",") == 1 and "@" not in text:
        last, first = (part.strip() for part in text.split(","))
        if last and first and first.rstrip(".").casefold() not in _NAME_SUFFIXES:
            return f"{first} {last}"
    return text


def is_decorated(name: str) -> bool:
    """True if the spelling carries a parenthetical or needs `Last, First`
    reordering, i.e. is not how the name would be displayed."""
    bare, notes = _split_notes(name.strip())
    return bool(notes) or _first_last(bare) != bare


def cleaner_name(current: str, incoming: str) -> str:
    """Of two spellings that key alike, the one to display: an undecorated
    spelling replaces a decorated one, otherwise `current` stands. An
    all-lowercase spelling never replaces a cased one: a handle-like
    `nikhil kandoi` is no improvement on `Kandoi, Nikhil`."""
    if (
        name_key(current) == name_key(incoming)
        and is_decorated(current)
        and not is_decorated(incoming)
        and not (incoming.islower() and not current.islower())
    ):
        return incoming
    return current


def _strip_latin_accents(name: str) -> str:
    """Drop combining marks after Latin letters only: they are accents there,
    but the dakuten that separates `ガ` from `カ` is part of the letter."""
    out: List[str] = []
    for ch in unicodedata.normalize("NFKD", name):
        if unicodedata.combining(ch) and out and ord(out[-1]) < _LATIN_LIMIT:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def login_as_name(login: str) -> Optional[str]:
    """The name key a GitHub login reads as, or None if it is not name-shaped.

    Only a login of two or more alphabetic words joined by `-` or `_`
    (`Jo-Levy`) qualifies; a single run of letters (`mirjak`) is far too
    likely to be a handle that merely resembles someone's name. This is a
    test of shape: `kmadhavan-msft` passes it and simply matches nobody.
    """
    parts = [p for p in _LOGIN_SEPARATOR_RE.split(login.strip()) if p]
    if len(parts) < 2 or not all(p.isalpha() and len(p) > 1 for p in parts):
        return None
    return name_key(" ".join(parts))


# Mailboxes that exist for a function, not a person. The IETF ones are only
# matched on IETF-run domains: plenty of people hold `name@ietf.org`.
_IETF_DOMAINS = {"ietf.org", "rfc-editor.org", "iab.org", "irtf.org", "iesg.org"}
_IETF_ROLE_LOCALPARTS = {
    "agenda",
    "chair",
    "iab",
    "iesg",
    "iesg-secretary",
    "ietf-action",
    "ietf-secretariat",
    "internet-drafts",
    "irsg",
    "rfc-editor",
    "secretariat",
    "session-request",
}
_AUTOMATED_LOCALPARTS = {
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "no-reply",
    "noreply",
    "postmaster",
}
_ROLE_NAMES: Set[str] = {
    name_key(n)
    for n in (
        "The IESG",
        "IESG",
        "IESG Secretary",
        "IAB",
        "IRSG",
        "IETF Chair",
        "IETF Secretariat",
        "Internet-Drafts",
        "IETF Meeting Session Request Tool",
        "Datatracker",
        "Independent Submissions Editor",
        "GitHub",
        "IAB Chair",
        "IETF Executive Director",
        "IAB Executive Administrative Manager",
        "Liaison Statement Management Tool",
        "IANA General Enquiries via RT",
        "ietf.org Support",
    )
}
_BOT_LOGIN_RE = re.compile(r"(?:\[bot\]|[-_]bot)$", re.IGNORECASE)
_BOT_LOGINS = {"dependabot", "github-actions", "renovate"}


def _is_role_address(address: str) -> bool:
    local, _, domain = address.lower().partition("@")
    if local in _AUTOMATED_LOCALPARTS:
        # GitHub relays a person's mail from `noreply@github.com` under their
        # own display name, so that address says nothing about who sent it.
        return domain != "github.com" and not domain.endswith(".github.com")
    return domain in _IETF_DOMAINS and local in _IETF_ROLE_LOCALPARTS


def _is_bot_login(login: str) -> bool:
    return bool(_BOT_LOGIN_RE.search(login)) or login.lower() in _BOT_LOGINS


def is_role_account(person: "Person") -> bool:
    """True for a function mailbox or bot rather than a human participant.

    Anyone holding a Datatracker role or a document credit is a person
    whatever their address looks like, so those are never reclassified.
    """
    if person.roles or person.authored_documents or person.edited_documents:
        return False
    key = name_key(person.canonical_name)
    # "Bot" alone could be a surname; the suffix rule wants a descriptive name.
    if key in _ROLE_NAMES or (key.endswith(" bot") and len(key.split()) >= 3):
        return True
    # Every address, not any: someone who also posts from a noreply@ mailbox
    # is still a person.
    if person.emails and all(_is_role_address(a) for a in person.emails):
        return True
    if "@" in person.canonical_name and _is_role_address(person.canonical_name):
        return True
    return any(_is_bot_login(login) for login in person.github_logins)
