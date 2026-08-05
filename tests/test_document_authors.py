"""Datatracker as the authority on document affiliation.

The point of this source: an author who states no `<organization>`
leaves an Authors' Addresses block that is textually identical to one
where their city sits in the organisation's place. Datatracker records
the empty string, which settles it — so a blank here has to *override*
the text parser rather than fall back to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import document_authors
from ietf_llm.log import Verbosity
from ietf_llm.paths import drafts_dir, get_wg_file_cache_dir
from ietf_llm.people import Registry, _ingest_draft_authors
from ietf_llm.people.affiliation import group_affiliations


def _row(doc: str, person: str, email: str, aff: str, order: int = 0) -> Dict[str, Any]:
    return {
        "document": f"/api/v1/doc/document/{doc}/",
        "person": f"/api/v1/person/person/{person}/",
        "email": f"/api/v1/person/email/{email}/",
        "affiliation": aff,
        "order": order,
    }


def _stub_api(
    monkeypatch: pytest.MonkeyPatch,
    rows: List[Dict[str, Any]],
    names: Dict[str, str],
) -> List[str]:
    """Serve both endpoints from canned data; returns the URLs requested."""
    seen: List[str] = []

    def fake_get(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        seen.append(url)
        if "documentauthor" in url:
            wanted = url.split("document__name__in=")[1].split("&")[0].split(",")
            return {
                "meta": {},
                "objects": [
                    r
                    for r in rows
                    if r["document"].rstrip("/").rsplit("/", 1)[-1] in wanted
                ],
            }
        return {
            "meta": {},
            "objects": [{"id": int(k), "name": v} for k, v in names.items()],
        }

    monkeypatch.setattr(document_authors, "_get_json", fake_get)
    return seen


def test_fetch_groups_by_document_and_resolves_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row("draft-a", "1", "mnot@mnot.net", "Cloudflare", order=1),
        _row("draft-a", "2", "alice@example.net", "", order=2),
        _row("draft-b", "1", "mnot@mnot.net", "Fastly"),
    ]
    _stub_api(monkeypatch, rows, {"1": "Mark Nottingham", "2": "Alice Chen"})
    out = document_authors.fetch_document_authors(
        ["draft-a", "draft-b"], verbose=Verbosity.QUIET
    )
    assert sorted(out) == ["draft-a", "draft-b"]
    assert [(a.name, a.affiliation) for a in out["draft-a"]] == [
        ("Mark Nottingham", "Cloudflare"),
        ("Alice Chen", ""),
    ]
    assert out["draft-b"][0].email == "mnot@mnot.net"


def test_fetch_batches_rather_than_one_request_per_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = [f"draft-{i:03d}" for i in range(90)]
    seen = _stub_api(monkeypatch, [_row(d, "1", "a@b.c", "X") for d in docs], {"1": "A"})
    document_authors.fetch_document_authors(docs, verbose=Verbosity.QUIET)
    author_calls = [u for u in seen if "documentauthor" in u]
    # 90 documents at 40 per request — three calls, not ninety.
    assert len(author_calls) == 3


def test_fetch_returns_empty_when_the_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The offline case, and the one every test in this suite exercises by
    # default: no rows, so the caller parses the text instead.
    monkeypatch.setattr(
        document_authors, "_get_json", lambda url, timeout=10.0: None
    )
    assert document_authors.fetch_document_authors(["draft-a"], Verbosity.QUIET) == {}


# --- through the registry --------------------------------------------------


def _write_draft(name: str, block: str, year_line: str) -> None:
    drafts = Path(drafts_dir(get_wg_file_cache_dir("wg")))
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"{name}.txt").write_text(
        f"HTTP                                          M. Nottingham\n"
        f"{year_line}\n\n\nAuthor's Address\n\n{block}\n"
    )


def test_blank_affiliation_overrides_the_text_parsers_guess(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole reason this source exists. The text says "Prahran" in the
    # organisation slot; Datatracker says the author stated none.
    _write_draft(
        "draft-ietf-wg-thing-02",
        "   Mark Nottingham\n   Prahran\n   Australia\n   Email: mnot@mnot.net\n",
        "Internet-Draft                                     16 May 2025",
    )
    _stub_api(
        monkeypatch,
        [_row("draft-ietf-wg-thing", "1", "mnot@mnot.net", "")],
        {"1": "Mark Nottingham"},
    )
    registry = Registry()
    _ingest_draft_authors("wg", registry, Verbosity.QUIET)
    person = registry.persons[0]
    assert person.authored_documents == {"draft-ietf-wg-thing"}
    assert group_affiliations(person) == []
    assert registry.affiliation_tag("Mark Nottingham") is None


def test_editor_status_still_comes_from_the_draft_text(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Datatracker records authorship but not who edited, so the `(ed.)`
    # marker in the text is the only source for it.
    _write_draft(
        "draft-ietf-wg-thing-02",
        "   Mark Nottingham (editor)\n   Prahran\n   Email: mnot@mnot.net\n",
        "Internet-Draft                                     16 May 2025",
    )
    _stub_api(
        monkeypatch,
        [_row("draft-ietf-wg-thing", "1", "mnot@mnot.net", "Cloudflare")],
        {"1": "Mark Nottingham"},
    )
    registry = Registry()
    _ingest_draft_authors("wg", registry, Verbosity.QUIET)
    person = registry.persons[0]
    assert person.edited_documents == {"draft-ietf-wg-thing"}
    assert person.authored_documents == set()
    assert registry.affiliation_tag("Mark Nottingham") == "Cloudflare"


def test_text_is_the_fallback_when_datatracker_has_no_row(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-IETF-stream documents, and every document when a gather runs
    # offline. The parser and its screening are what is left.
    _write_draft(
        "draft-ietf-wg-thing-02",
        "   Mark Nottingham\n   Cloudflare\n   Prahran VIC\n"
        "   Australia\n   Email: mnot@mnot.net\n",
        "Internet-Draft                                     16 May 2025",
    )
    monkeypatch.setattr(
        document_authors, "_get_json", lambda url, timeout=10.0: None
    )
    registry = Registry()
    _ingest_draft_authors("wg", registry, Verbosity.QUIET)
    person = registry.persons[0]
    assert registry.affiliation_tag("Mark Nottingham") == "Cloudflare"
    # The address lines are still recorded, because the corroboration is
    # what the fallback path relies on.
    assert "prahran" in person.localities
