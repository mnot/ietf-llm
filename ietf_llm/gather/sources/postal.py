"""Tell an organisation line apart from a postal-address line.

The Authors' Addresses block of an I-D / RFC is plain text with no
markup separating the author's organisation from their street address:

    Mark Nottingham          <- name
    Cloudflare               <- organisation
    Prahran VIC              <- city / region
    Australia                <- country

An author who gives no `<organization>` produces the *same shape* minus
one line, so the line under the name is a city rather than an employer:

    Mark Nottingham
    Prahran
    Australia

Reading that first line as the organisation is what put "Prahran",
"Melbourne" and "Burlingame, CA  94010" into the people registry
alongside real employers. This module holds the classifier that keeps
address lines out: countries, street lines, postcodes, phone numbers,
and the rendering artefacts (`made in`, `true, 1`) that some author XML
leaks into the text output.

One residue is left on purpose: a bare city with no other signal
("Prahran") is lexically indistinguishable from a one-word employer
("Fastly"), so the classifier lets it through and `people/` suppresses it
separately, using the same person's *other* documents as corroboration
(the `address_lines` that `Registry.add_document_author` folds into
`Person.localities`).
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional

# Multi-word country names, matched as a whole phrase after `_flatten`.
_COUNTRY_PHRASES = frozenset(
    {
        "bosnia and herzegovina",
        "costa rica",
        "czech republic",
        "dominican republic",
        "el salvador",
        "hong kong",
        "new zealand",
        "north macedonia",
        "peoples republic of china",
        "pr china",
        "puerto rico",
        "republic of korea",
        "russian federation",
        "saudi arabia",
        "south africa",
        "south korea",
        "sri lanka",
        "the netherlands",
        "united arab emirates",
        "united kingdom",
        "united states",
        "united states of america",
    }
)

# Single-word country names and the ISO-ish abbreviations that appear in
# the corpus. Compared after `_flatten`, so "P.R.China", "U.S.A" and
# "FINLAND" all collapse onto an entry here.
_COUNTRY_WORDS = frozenset("""
    afghanistan albania algeria andorra angola argentina armenia australia
    austria azerbaijan bahrain bangladesh belarus belgium bolivia botswana
    brazil brunei bulgaria cambodia cameroon canada chile china colombia
    croatia cuba cyprus czechia denmark ecuador egypt england estonia
    ethiopia finland france georgia germany ghana greece guatemala guernsey
    honduras hungary iceland india indonesia iran iraq ireland israel italy
    jamaica japan jordan kazakhstan kenya korea kuwait latvia lebanon
    liechtenstein lithuania luxembourg macao malaysia malta mauritius mexico
    moldova monaco mongolia montenegro morocco myanmar nepal netherlands
    nicaragua nigeria norway oman pakistan palestine panama paraguay peru
    philippines poland portugal qatar romania russia scotland senegal
    serbia singapore slovakia slovenia spain sweden switzerland syria taiwan
    tanzania thailand tunisia turkey turkiye uganda ukraine uruguay
    uzbekistan venezuela vietnam wales zambia zimbabwe
    ar at au be br ca ch cn cz de dk es fi fr gb gr hk hu ie il in it jp kr
    lu mx nl no nz pl pt ro ru se sg tr tw ua uk us usa
    """.split())

# Region / state / province abbreviations that terminate a city line
# ("Prahran VIC", "Reston, VA"). Only consulted for a short line with no
# corporate-form token, so an org name ending in one of these survives.
_REGION_ABBREVS = frozenset("""
    al ak az ar ca co ct de fl ga hi id il ia ks ky la me md ma mi mn ms
    mo mt ne nv nh nj nm ny nc nd oh ok pa ri sc sd tn tx ut vt va wa wi
    wy dc
    ab bc mb nb nl ns nt nu on pe qc sk yt
    nsw vic qld tas act
    """.split())

# Legal-form and descriptor tokens that mark a line as a company name
# rather than a place; they veto the region-abbreviation rule.
_ORG_FORM_TOKENS = frozenset("""
    inc incorporated llc llp lp ltd limited gmbh ag ab as bv nv sa sas srl
    spa plc oy oyj kk pty co company corp corporation group holdings
    technologies technology systems labs laboratories university institute
    consulting foundation project
    """.split())

# The organisational units that authors put in the organisation slot when
# they work at an institution. Kept separate from `_ORG_FORM_TOKENS`
# because these words do occur in place names ("College Park, MD") and so
# must not veto the region-abbreviation rule — but a line naming one is
# still never safe to record as a *locality*, since doing so would
# suppress the same string where another draft states it as the
# organisation. See `names_an_organisation`.
_ORG_UNIT_TOKENS = _ORG_FORM_TOKENS | frozenset(
    "school department dept faculty college academy".split()
)

_MONTHS = frozenset("""
    january february march april may june july august september october
    november december
    """.split())

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# House number leading a street line: "6011 W Courtyard Dr.",
# "1770 Massachusetts Ave, #322", "17/4 Krylatskaya st". A single digit
# glued to a letter is not a house number — "4K Associates" is a company.
_STREET_NUMBER_RE = re.compile(r"^(?:\d+[/-]\d+|\d{2,}[A-Za-z]?|\d+)[\s,]")
# … but a leading number alone is not enough: "128 Technology" and
# "802 Secure" are companies. A street line also runs to three or more
# words ("499 Park Avenue") or names a thoroughfare ("17/4 Krylatskaya
# st"), and a two-word company name does neither.
_STREET_TYPES = frozenset("""
    st street rd road ave avenue av blvd boulevard bd dr drive ln lane
    way pl place ct court cir circle sq square ter terrace pkwy parkway
    hwy highway close crescent walk row quay wharf strasse str allee weg
    platz chemin rue via viale corso calle carrera laan straat gatan
    """.split())
_PO_BOX_RE = re.compile(r"^(?:p\.?\s*o\.?\s*box|post\s+office\s+box)\b", re.IGNORECASE)
# Postcodes: 4-6 digit runs (US ZIP, most of Europe and Asia) plus the
# Canadian and UK alphanumeric forms.
_POSTCODE_RE = re.compile(
    r"\b\d{4,6}(?:-\d{4})?\b"
    r"|\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b"
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b"
)
_PHONE_RE = re.compile(r"^[\d\s()+.\-]{7,}$")
# A region abbreviation standing on its own ("VIC", "VA.", "CA,") rather
# than embedded in a name ("(TAS)").
_BARE_ABBREV_RE = re.compile(r"^[A-Za-z]{2,3}[.,]?$")
# "true, 1" and friends: a rendering artefact trailing a bare small
# integer after a comma. No organisation name is spelled that way.
_TRAILING_INDEX_RE = re.compile(r",\s*\d{1,3}$")


def _flatten(line: str) -> str:
    """Lowercase, replace punctuation with a space, collapse whitespace."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", line.lower())).strip()


def _squash(line: str) -> str:
    """Lowercase, *delete* punctuation, collapse whitespace.

    The counterpart to `_flatten` for abbreviations written with stops:
    "U.S.A" squashes to `usa` where flattening gives `u s a`, and
    "P.R. China" to `pr china`.
    """
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", line.lower())).strip()


def is_country_line(line: str) -> bool:
    """True when the whole line is just a country name or abbreviation."""
    return any(
        form in _COUNTRY_WORDS or form in _COUNTRY_PHRASES
        for form in (_flatten(line), _squash(line))
        if form
    )


def _is_contact_or_debris(stripped: str) -> bool:
    """Contact detail or rendering debris that leaked into the block."""
    words = stripped.split()
    return (
        "@" in stripped
        or stripped.endswith(":")
        or stripped.lower().startswith(("http://", "https://", "www."))
        or _TRAILING_INDEX_RE.search(stripped) is not None
        or (
            _PHONE_RE.match(stripped) is not None
            and sum(ch.isdigit() for ch in stripped) >= 7
        )
        # An all-lowercase multi-*word* ASCII fragment ("made in") is
        # postal-line debris from the author's XML. Split on whitespace,
        # not punctuation, so a lowercase domain-style name ("yes.com")
        # stays one word; a single lowercase word is left alone either
        # way, because "independent", "deSEC" and "sn3rd" are real
        # self-descriptions.
        or (len(words) > 1 and stripped == stripped.lower() and stripped.isascii())
    )


def _is_street_line(stripped: str, words: List[str], tokens: List[str]) -> bool:
    """A house number *and* something that makes it a street, not a name."""
    if _STREET_NUMBER_RE.match(stripped) is None:
        return False
    return len(words) >= 3 or any(token in _STREET_TYPES for token in tokens)


def _is_geographic(stripped: str) -> bool:
    """Country, street line, postcode, or city + region abbreviation."""
    words = stripped.split()
    tokens = _flatten(stripped).split()
    return (
        is_country_line(stripped)
        or _PO_BOX_RE.match(stripped) is not None
        or _is_street_line(stripped, words, tokens)
        or _POSTCODE_RE.search(stripped) is not None
        # "Prahran VIC", "Reston, VA": a short line whose last word is a
        # bare region abbreviation, with nothing in it that reads as a
        # company. The raw word has to *be* the abbreviation — "Thales
        # Alenia Space (TAS)" is a company, not a place in Tasmania.
        or (
            2 <= len(words) <= 3
            and bool(tokens)
            and tokens[-1] in _REGION_ABBREVS
            and _BARE_ABBREV_RE.match(words[-1]) is not None
            and not any(token in _ORG_FORM_TOKENS for token in tokens)
        )
    )


def looks_like_postal_line(line: str) -> bool:
    """True when `line` is address / contact detail, not an organisation.

    Deliberately one-sided. A false *negative* leaves a city in the
    affiliation set — visible, and often caught later by corroboration —
    while a false *positive* silently discards a real employer. So every
    rule here keys off something an organisation name does not normally
    contain: a country standing alone, a house number, a postcode, a
    phone number, an `@`.
    """
    stripped = line.strip()
    if not stripped or not _flatten(stripped):
        return True
    return _is_contact_or_debris(stripped) or _is_geographic(stripped)


def names_an_organisation(line: str) -> bool:
    """True when the line carries a token only an organisation name has.

    The counterweight to `locality_key`. Author blocks routinely put an
    institution on an address line — under a department in the
    organisation slot ("Department of Computer Science" over "University
    of Auckland"), or simply repeating the organisation inside its own
    postal address. Treating those as places would let one document's
    address suppress another document's *employer*, which is exactly the
    silent loss `looks_like_postal_line` is built to avoid. Callers use
    this to keep such lines out of `Person.localities`.
    """
    return any(token in _ORG_UNIT_TOKENS for token in _flatten(line).split())


def locality_key(line: str) -> str:
    """Normalise a place name for cross-document comparison.

    "Prahran VIC" and "Prahran" both reduce to `prahran`, so a city
    confirmed by one document — where it sat *below* an organisation line
    — can suppress the same city misread as an organisation in another.
    Region abbreviations and bare numbers are dropped; everything else is
    kept.
    """
    tokens = [
        token
        for token in _flatten(line).split()
        if not token.isdigit() and token not in _REGION_ABBREVS
    ]
    return " ".join(tokens)


_DATE_RES = (
    # "16 May 2025"
    re.compile(r"\b\d{1,2}\s+([A-Z][a-z]+),?\s+(\d{4})\b"),
    # "February 6, 2014"
    re.compile(r"\b([A-Z][a-z]+)\s+\d{1,2},\s*(\d{4})\b"),
    # "September 2024" / "October, 2002" (RFC and older I-D front matter)
    re.compile(r"\b([A-Z][a-z]+),?\s+(\d{4})\b"),
)
# The front matter is two columns separated by a run of spaces.
_COLUMN_SPLIT_RE = re.compile(r"\s{2,}")
# Leading blanks may carry a BOM or a form feed rather than plain spaces.
_BLANKS = "﻿\f \t\r\n"


def _header_lines(text: str) -> Iterator[str]:
    """The front-matter block only: up to the blank line that ends it.

    Scanning further reaches the abstract and the IETF Trust boilerplate,
    both of which contain dates ("…before November 10, 2008.") that are
    not the document's own.
    """
    started = False
    for raw in text.splitlines()[:30]:
        if not raw.strip(_BLANKS):
            if started:
                return
            continue
        started = True
        yield raw


def parse_document_year(text: str) -> Optional[int]:
    """Year from an I-D / RFC front-matter date line, or None.

    The publication date sits in the right-hand column of the header
    block: `16 May 2025` on a draft, `September 2024` on an RFC. The
    left-hand column often carries an expiry date six months later — and
    on older drafts both sit on the *same* line — so the header is split
    into columns and any column introduced by `Expires` / `Expiration
    Date` is dropped before the scan.

    The *first* surviving date wins. Where a header carries two, the
    document's own date leads and the expiry follows, even when the
    expiry isn't labelled as one.

    Used only to order a person's affiliations by recency, so a miss
    costs ordering, not correctness — but a wrong year is worse than a
    miss, because it can push a current employer past the render cap.
    """
    for raw in _header_lines(text):
        for column in _COLUMN_SPLIT_RE.split(raw.strip()):
            if not column or column.lower().startswith("expir"):
                continue
            for pattern in _DATE_RES:
                for match in pattern.finditer(column):
                    month, year_text = match.groups()
                    if month.lower() not in _MONTHS:
                        continue
                    year = int(year_text)
                    if 1990 <= year <= 2100:
                        return year
    return None
