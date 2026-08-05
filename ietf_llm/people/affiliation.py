"""Collapse and rank a person's affiliations for display.

A twenty-five-year IETF career leaves a person with a dozen stated
employers across their drafts, several of which are the same company
spelled three ways:

    Fastly; Cloudflare; Akamai; Akamai Technologies; Akamai
    Technologies, Inc.; Rackspace; Yahoo! Inc.; BEA Systems

Rendering that inline on a thread's Participants line is unusable, so
this module does two things before any renderer sees the values:

  - **collapse near-duplicates.** `org_key` strips legal-form and
    generic descriptor suffixes ("Inc.", "LLC", "Technologies",
    "Systems") and case/punctuation, so the three Akamai spellings share
    one key. The variant used in the most documents becomes the display
    form (ties to the shortest), which is normally the plain company
    name.

  - **rank by recency and cap.** Groups sort newest-first on the
    publication year of the documents backing them, so the current
    employer leads. Renderers take the first few and say how many older
    ones they dropped — a person's affiliation history is real, but the
    inline line is not where it belongs.

Undated sources (the GitHub `company` field) sort after dated ones
rather than being treated as current: a stale profile shouldn't
outrank a draft published last month.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from ..gather.sources.postal import locality_key

if TYPE_CHECKING:
    from . import Person

#: Inline renderings (the thread Participants line) show this many
#: organisations before summarising the rest.
INLINE_CAP = 3
#: The people digest's Affiliation column has more room, but not
#: unlimited: it is a table cell, not a CV.
TABLE_CAP = 5

# Trailing tokens dropped when computing the collapse key. Legal forms
# ("Inc", "GmbH") plus the handful of generic descriptors that actually
# produce duplicate spellings in the corpus ("Akamai Technologies" /
# "Akamai", "Cisco Systems" / "Cisco"). Deliberately short: every entry
# here risks merging two genuinely different organisations.
_TRAILING_NOISE = frozenset("""
    inc incorporated llc llp lp ltd limited gmbh ag ab as bv nv sa sas srl
    spa plc oy oyj kk pty co company corp corporation
    technologies technology systems
    """.split())

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def org_key(org: str) -> str:
    """Collapse key for one organisation string.

    "Akamai", "Akamai Technologies" and "Akamai Technologies, Inc." all
    reduce to `akamai`. Returns the fully-normalised string when stripping
    would leave nothing — or nothing but digits — so an organisation
    actually named "Systems" still gets a key of its own, and
    "128 Technology" keys as itself rather than as the bare number `128`.
    """
    flat = _WS_RE.sub(" ", _PUNCT_RE.sub(" ", org.lower())).strip()
    tokens = flat.split()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    while len(tokens) > 1 and tokens[-1] in _TRAILING_NOISE:
        tokens.pop()
    key = " ".join(tokens)
    if not key or not any(ch.isalpha() for ch in key):
        return flat
    return key


@dataclass
class AffiliationGroup:
    """One organisation, with every spelling and source that named it."""

    display: str
    sources: Set[str] = field(default_factory=set)
    documents: int = 0
    #: Publication year of the most recent document naming this org;
    #: None when every source backing it is undated (GitHub).
    year: Optional[int] = None


def group_affiliations(person: "Person") -> List[AffiliationGroup]:
    """Distinct organisations for `person`, most recent first.

    Near-duplicate spellings collapse into one group. Values that the
    person's own documents show to be a place rather than an employer
    (see `Person.localities`) are dropped: a bare city under the name in
    the Authors' Addresses block looks exactly like a one-word company,
    and the only thing that tells them apart is having seen the same
    string sit *below* an organisation line somewhere else.
    """
    variants: Dict[str, Dict[str, int]] = {}
    groups: Dict[str, AffiliationGroup] = {}
    for source_key, org in person.affiliations.items():
        org = (org or "").strip()
        if not org or locality_key(org) in person.localities:
            continue
        key = org_key(org)
        group = groups.setdefault(key, AffiliationGroup(display=org))
        group.sources.add(source_key.split(":", 1)[0])
        group.documents += 1
        year = person.affiliation_years.get(source_key)
        if year is not None and (group.year is None or year > group.year):
            group.year = year
        counts = variants.setdefault(key, {})
        counts[org] = counts.get(org, 0) + 1
    for key, group in groups.items():
        # Prefer the spelling used most, then the shortest — which is
        # normally the bare company name rather than its legal form.
        group.display = min(
            variants[key].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0])
        )[0]
    return sorted(
        groups.values(),
        key=lambda g: (-(g.year or 0), -g.documents, g.display.lower()),
    )


def capped(
    groups: List[AffiliationGroup], cap: int
) -> "tuple[List[AffiliationGroup], int]":
    """Split `groups` into the ones to show and a count of the rest."""
    if len(groups) <= cap:
        return (groups, 0)
    return (groups[:cap], len(groups) - cap)


def overflow_note(dropped: int) -> str:
    """Trailing marker for affiliations a cap left out."""
    return f" (+{dropped} earlier)" if dropped else ""
