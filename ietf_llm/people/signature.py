"""Affiliation from a display-name parenthetical, source label `signature`.

Some mail clients put the sender's organisation in the display name:
`Deen, Glenn (Comcast Cable)`, `Martin Pels (RIPE NCC)`. Most trailing
parentheticals are not organisations, though: a Datatracker handle
(`Eric Vyncke (evyncke)`), a nickname (`Dongjie (Jimmy)`), a department code
(`maqiufang (A)`), a status (`Dang, Quynh H. (Fed)`), or a person's other name
(`Independent Submissions Editor (Eliot Lear)`). A wrong organisation is worse
than a blank, so a parenthetical becomes an affiliation only when it passes a
shape test, and a single capitalised word, which is what a nickname also looks
like, needs outside corroboration besides.

This runs after the registry is assembled because the corroboration is
registry-wide: other people's organisations, everyone's names, and how many
people use the same parenthetical.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Set

from ..log import LogLevel, Verbosity, log
from .affiliation import _TRAILING_NOISE, org_key
from .names import is_role_account, name_key, without_parentheticals

if TYPE_CHECKING:
    from . import Person, Registry

_MAX_TOKENS = 4
_MIN_LETTERS = 3
#: Beyond Latin Extended-B: CJK and other scripts cannot be told from a
#: personal name, so they are left out rather than guessed at.
_LATIN_LIMIT = 0x250
_NOT_AN_ORG_FIRST_WORDS = {"ietf", "iab", "iesg", "irtf", "via", "sent"}
_NOT_AN_ORG_WORDS = {
    # Status and arrangement, not employer.
    "fed",
    "civ",
    "lcdr",
    "external",
    "internal",
    "personal",
    "private",
    "work",
    "home",
    "mobile",
    "remote",
    "retired",
    "guest",
    "student",
    "intern",
    "contractor",
    "consultant",
    # A role or title.
    "chair",
    "chairs",
    "co-chair",
    "ad",
    "editor",
    "secretary",
    "director",
    "manager",
    "engineer",
    "architect",
    "servers",
}
#: Words that make a two-to-four word parenthetical read as an organisation
#: rather than as someone's other name.
_ORG_DESCRIPTORS = _TRAILING_NOISE | frozenset("""
    cable networks network communications telecom telekom group labs lab
    laboratory university institute foundation bank software security cloud
    research services solutions consulting media partners association council
    centre center ministry department agency society alliance consortium
    """.split())
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _letters(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha())


def _has_acceptable_shape(text: str) -> bool:
    """The checks that need nothing but the string."""
    letters = _letters(text)
    tokens = text.split()
    if len(letters) < _MIN_LETTERS or not 1 <= len(tokens) <= _MAX_TOKENS:
        return False
    if any(ord(ch) >= _LATIN_LIMIT for ch in letters):
        return False
    if not text[0].isupper() or any(ch in text for ch in ",@:"):
        return False
    if tokens[0].casefold() in _NOT_AN_ORG_FIRST_WORDS:
        return False
    return not any(w.casefold() in _NOT_AN_ORG_WORDS for w in _WORD_RE.findall(text))


def _is_acronym(text: str) -> bool:
    letters = _letters(text)
    return len(letters) >= 4 and letters.isupper()


def _reads_as_org(text: str) -> bool:
    """A lone acronym, or several words with an acronym or an organisational
    descriptor among them. A bare capitalised word, or two (`Eliot Lear`), is
    exactly what a nickname or another name looks like."""
    words = _WORD_RE.findall(text)
    if len(text.split()) == 1:
        return _is_acronym(text)
    return any(w.isupper() and len(w) >= 2 for w in words) or any(
        w.casefold() in _ORG_DESCRIPTORS for w in words
    )


def _restates_the_name(text: str, person: "Person") -> bool:
    """True when the parenthetical is part of the person's own name:
    `Pengshuping (Peng Shuping)`."""
    bare = without_parentheticals(person.canonical_name)
    own = [w.casefold() for w in _WORD_RE.findall(bare)]
    words = [w.casefold() for w in _WORD_RE.findall(text)]
    if any(w in own and len(w) >= 3 for w in words):
        return True
    # `Pengshuping (Peng Shuping)`: the same letters, split differently.
    return "".join(words) == "".join(own)


def _domain_labels(person: "Person") -> Set[str]:
    return {
        label
        for domain in person.email_domains
        for label in re.split(r"[.\-]", domain.casefold())
        if len(label) >= 4
    }


def apply_signature_affiliations(registry: "Registry", verbose: Verbosity) -> None:
    """Record `signature` affiliations from screened display-name parentheticals.

    Stored under `signature:<org key>`, undated, so a draft's organisation
    (which carries a year) always leads and an organisation both name reads
    `Nokia (draft, signature)`.
    """
    persons = registry.all_persons()
    if not any(p.name_parentheticals for p in persons):
        return
    people_names = {name_key(p.canonical_name) for p in persons}
    known_orgs = {
        org_key(org)
        for p in persons
        for source, org in p.affiliations.items()
        if not source.startswith("signature:")
    }
    users: "Counter[str]" = Counter(
        org_key(text) for p in persons for text in p.name_parentheticals
    )
    recorded = 0
    for person in persons:
        if is_role_account(person):
            continue
        labels = _domain_labels(person)
        for text in sorted(person.name_parentheticals):
            text = " ".join(text.split())
            if not _has_acceptable_shape(text) or name_key(text) in people_names:
                continue
            if _restates_the_name(text, person):
                continue
            key = org_key(text)
            if not _reads_as_org(text):
                corroborated = (
                    key in known_orgs
                    or users[key] >= 2
                    or any(w.casefold() in labels for w in _WORD_RE.findall(text))
                )
                if not corroborated:
                    continue
            person.affiliations[f"signature:{key}"] = text
            recorded += 1
    if recorded:
        log(
            f"  display-name affiliations: {recorded}",
            verbose,
            level=LogLevel.PROGRESS,
        )
