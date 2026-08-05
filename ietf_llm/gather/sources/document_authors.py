"""Authoritative per-document authorship and affiliation from Datatracker.

The Authors' Addresses block of an I-D is prose: an author's organisation
and their street address sit in the same undifferentiated column of text,
and an author who states no organisation leaves a block that is
*indistinguishable* from one where the city is the employer. `postal.py`
exists to guess at that; this module removes the need to guess for any
document Datatracker knows, because the submission tool captured
`<organization>` as a structured field and the API hands it back:

    {"affiliation": "Cloudflare", "country": "", "order": 1,
     "person": "/api/v1/person/person/103881/",
     "email": "/api/v1/person/email/mnot@mnot.net/"}

An **empty** `affiliation` is data, not a gap: it records that the author
stated no organisation. That is exactly the case the text parser cannot
see, and it is why this source has to be consulted per *document* rather
than merely used to fill blanks — a blank here overrides a city the text
parser would otherwise have offered as an employer.

Measured against every document in the local caches (988 drafts and
RFCs): 100% have rows, and the text parser and Datatracker disagree on
seven author records, five of which are the text parser reading a
suburb as an employer. Blank rates run 2-5% from 2005 on, rising for
pre-2005 documents where authors genuinely stated less often.

Two endpoints, both batched by name/id so a corpus costs a handful of
requests rather than one per document — 79 httpbis drafts resolve in two
`documentauthor` calls plus a couple for the person names. Everything is
best-effort: any failure yields no rows for the affected documents and
the caller falls back to parsing the text, which is also what happens
when a gather runs with no network at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ...log import LogLevel, Verbosity, log
from .datatracker import _get_json

_DOCAUTHOR_API = "https://datatracker.ietf.org/api/v1/doc/documentauthor/"
_PERSON_API = "https://datatracker.ietf.org/api/v1/person/person/"

#: Names per request. Keeps the URL well inside any sane limit while
#: holding a whole working group to two or three calls.
_BATCH = 40
#: Rows per page. A 40-document batch has never needed a second page in
#: the corpora measured, but `meta.next` is followed regardless.
_PAGE = 1000


@dataclass
class DocumentAuthor:
    """One author of one document, as Datatracker records them."""

    name: str
    email: Optional[str]
    #: The organisation the author stated. `""` means they stated none —
    #: a real answer, and the one the text parser gets wrong. Never None:
    #: a document Datatracker has nothing for yields no `DocumentAuthor`
    #: at all rather than a row with an unknown affiliation.
    affiliation: str
    order: int


def _tail(uri: object) -> str:
    """Last path segment of an API URI (`/…/person/103881/` → `103881`)."""
    return str(uri or "").rstrip("/").rsplit("/", 1)[-1]


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _fetch_rows(names: Sequence[str]) -> List[Dict[str, Any]]:
    """Every documentauthor row for `names`, or [] if the fetch fails."""
    rows: List[Dict[str, Any]] = []
    for chunk in _chunks(names, _BATCH):
        url: Optional[str] = (
            f"{_DOCAUTHOR_API}?document__name__in={','.join(chunk)}&limit={_PAGE}"
        )
        while url:
            body = _get_json(url)
            if not body:
                # Best-effort: this chunk simply contributes nothing and
                # its documents fall back to the text parser.
                break
            rows.extend(body.get("objects") or [])
            url = (body.get("meta") or {}).get("next") or None
    return rows


def _fetch_person_names(person_ids: Sequence[str]) -> Dict[str, str]:
    """Map Datatracker person id → display name, best-effort."""
    names: Dict[str, str] = {}
    for chunk in _chunks(person_ids, _BATCH):
        body = _get_json(f"{_PERSON_API}?id__in={','.join(chunk)}&limit={_PAGE}")
        if not body:
            continue
        for obj in body.get("objects") or []:
            ident, name = obj.get("id"), obj.get("name")
            if ident is not None and name:
                names[str(ident)] = str(name)
    return names


def fetch_document_authors(
    document_names: Sequence[str], verbose: Verbosity = Verbosity.STATUS
) -> Dict[str, List[DocumentAuthor]]:
    """Authorship for each of `document_names`, keyed by document name.

    A document missing from the result is one Datatracker had nothing
    for — the caller should parse its text instead. A document *present*
    is authoritative, including for authors whose affiliation is `""`.
    """
    if not document_names:
        return {}
    rows = _fetch_rows(sorted(set(document_names)))
    if not rows:
        log(
            "  no Datatracker authorship available; "
            "reading affiliations from draft text",
            verbose,
            level=LogLevel.PROGRESS,
        )
        return {}

    person_names = _fetch_person_names(
        sorted({_tail(row.get("person")) for row in rows if row.get("person")})
    )
    out: Dict[str, List[DocumentAuthor]] = {}
    for row in rows:
        document = _tail(row.get("document"))
        name = person_names.get(_tail(row.get("person")), "")
        email = _tail(row.get("email")) or None
        if not document or not (name or email):
            continue
        out.setdefault(document, []).append(
            DocumentAuthor(
                name=name,
                email=email,
                affiliation=str(row.get("affiliation") or "").strip(),
                order=int(row.get("order") or 0),
            )
        )
    for authors in out.values():
        authors.sort(key=lambda a: a.order)
    log(
        f"  Datatracker authorship for {len(out)}/{len(set(document_names))} "
        f"documents ({len(rows)} author records)",
        verbose,
        level=LogLevel.PROGRESS,
    )
    return out
