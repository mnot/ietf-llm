"""Tests for gathering the reviews a person wrote.

Datatracker is mocked at the `_get_json` seam and the review-page fetch
at `fetch_resource`, so these exercise multi-address expansion, the
completed-only filter, request/team batching, the `<pre class="pasted">`
scrape (including the request-comment decoy), and rendering — without
touching the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ietf_llm.gather.sources import reviews as reviews_module
from ietf_llm.gather.sources.reviews import (
    Review,
    fetch_review_text,
    fetch_reviews,
    render_review,
    write_review_files,
)
from ietf_llm.log import Verbosity

Q = Verbosity.QUIET


# --- Mock helpers ---------------------------------------------------------


def _stub_get_json(
    monkeypatch: pytest.MonkeyPatch, responses: Dict[str, Any]
) -> List[str]:
    """Canned `_get_json` keyed by URL substring; records call URLs."""
    calls: List[str] = []

    def fake(url: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        calls.append(url)
        for key, body in responses.items():
            if key in url:
                return body
        return None

    monkeypatch.setattr(reviews_module, "_get_json", fake)
    return calls


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


def _stub_page(monkeypatch: pytest.MonkeyPatch, pages: Dict[str, str]) -> None:
    def fake(url: str, headers: Any = None) -> Optional[_Response]:
        for key, body in pages.items():
            if key in url:
                return _Response(body)
        return None

    monkeypatch.setattr(reviews_module, "fetch_resource", fake)


def _assignment(
    ident: int, review: Optional[str], state: str, request_id: int, rev: str = "09"
) -> Dict[str, Any]:
    return {
        "id": ident,
        "review": review,
        "reviewed_rev": rev,
        "result": "/api/v1/name/reviewresultname/ready-issues/",
        "assigned_on": "2023-12-18T06:48:46Z",
        "completed_on": "2023-12-29T08:57:44Z",
        "review_request": f"/api/v1/review/reviewrequest/{request_id}/",
        "state": f"/api/v1/name/reviewassignmentstatename/{state}/",
    }


_REVIEW_DOC = "review-ietf-ppm-dap-09-httpdir-early-nottingham-2023-12-29"


def _base_responses(assignments: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "/person/email/?person=": {
            "objects": [{"address": "a@example.com"}, {"address": "b@example.org"}]
        },
        "reviewer=a@example.com": {"objects": assignments, "meta": {"next": None}},
        "reviewer=b@example.org": {"objects": [], "meta": {"next": None}},
        "/review/reviewrequest/?id__in=": {
            "objects": [
                {
                    "id": 18540,
                    "doc": "/api/v1/doc/document/draft-ietf-ppm-dap/",
                    "team": "/api/v1/group/group/2351/",
                    "type": "/api/v1/name/reviewtypename/early/",
                    "comment": "Requested for issue 450.",
                }
            ]
        },
        "/group/group/?id__in=": {"objects": [{"id": 2351, "acronym": "httpdir"}]},
    }


# --- fetch_reviews --------------------------------------------------------


def test_fetch_reviews_expands_all_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both of the person's addresses are queried — a review filed under
    an old employer address must not be missed."""
    assignment = _assignment(
        1, f"/api/v1/doc/document/{_REVIEW_DOC}/", "completed", 18540
    )
    calls = _stub_get_json(monkeypatch, _base_responses([assignment]))
    result = fetch_reviews(103881, "Mark Nottingham", Q)

    assert [r.doc_name for r in result] == [_REVIEW_DOC]
    assert any("reviewer=a@example.com" in c for c in calls)
    assert any("reviewer=b@example.org" in c for c in calls)


def test_fetch_reviews_populates_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    assignment = _assignment(
        1, f"/api/v1/doc/document/{_REVIEW_DOC}/", "completed", 18540
    )
    _stub_get_json(monkeypatch, _base_responses([assignment]))
    review = fetch_reviews(103881, "Mark Nottingham", Q)[0]

    assert review.reviewer == "Mark Nottingham"
    assert review.reviewed_doc == "draft-ietf-ppm-dap"
    assert review.reviewed_rev == "09"
    assert review.reviewed_label == "draft-ietf-ppm-dap-09"
    assert review.team == "httpdir"
    assert review.review_type == "early"
    assert review.result == "ready-issues"
    assert review.request_comment == "Requested for issue 450."
    assert review.url.endswith(f"/{_REVIEW_DOC}/")


def test_fetch_reviews_skips_incomplete_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assigned-but-never-written and declined assignments have no text;
    only `completed` assignments with a review document survive."""
    assignments = [
        _assignment(1, None, "no-response", 18540),
        _assignment(2, None, "assigned", 18540),
        # 'completed' but with a null review doc — defensive: skip it too.
        _assignment(3, None, "completed", 18540),
        _assignment(4, f"/api/v1/doc/document/{_REVIEW_DOC}/", "completed", 18540),
    ]
    _stub_get_json(monkeypatch, _base_responses(assignments))
    assert len(fetch_reviews(103881, "Mark Nottingham", Q)) == 1


def test_fetch_reviews_dedupes_shared_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same assignment id returned for two addresses is one review."""
    assignment = _assignment(
        7, f"/api/v1/doc/document/{_REVIEW_DOC}/", "completed", 18540
    )
    responses = _base_responses([assignment])
    responses["reviewer=b@example.org"] = {
        "objects": [assignment],
        "meta": {"next": None},
    }
    _stub_get_json(monkeypatch, responses)
    assert len(fetch_reviews(103881, "Mark Nottingham", Q)) == 1


def test_fetch_reviews_empty_when_person_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_get_json(monkeypatch, _base_responses([]))
    assert fetch_reviews(103881, "Nobody", Q) == []


# --- fetch_review_text ----------------------------------------------------


def _review(comment: str = "") -> Review:
    return Review(
        doc_name=_REVIEW_DOC,
        reviewer="Mark Nottingham",
        reviewed_doc="draft-ietf-ppm-dap",
        reviewed_rev="09",
        team="httpdir",
        review_type="early",
        result="ready-issues",
        assigned_on="2023-12-18T06:48:46Z",
        completed_on="2023-12-29T08:57:44Z",
        request_comment=comment,
    )


def test_fetch_review_text_prefers_body_over_request_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requester's note renders in a `pre.pasted` block *before* the
    review body. Taking the first block would capture the wrong text."""
    page = (
        '<html><pre class="pasted">Requested for issue 450.</pre>'
        '<pre class="pasted">The actual review body.</pre></html>'
    )
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review("Requested for issue 450.")) == (
        "The actual review body."
    )


def test_fetch_review_text_unescapes_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = '<pre class="pasted">use &lt;pre&gt; &amp; such</pre>'
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review()) == "use <pre> & such"


def test_fetch_review_text_strips_datatracker_linkification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Datatracker turns document references inside the block into
    anchors; the corpus wants the reference text, not the markup."""
    page = (
        '<pre class="pasted">a mapping of <a href="/doc/rfc6690/">RFC6690</a> '
        "into JSON</pre>"
    )
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review()) == "a mapping of RFC6690 into JSON"


def test_fetch_review_text_keeps_escaped_angle_brackets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tag-stripping must run *before* unescaping: a review that quotes
    `<Content-Type>` sends it escaped, and stripping after unescaping
    would delete it as if it were markup."""
    page = (
        '<pre class="pasted">the &lt;Content-Type&gt; field, see '
        '<a href="/doc/rfc9110/">RFC9110</a></pre>'
    )
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review()) == "the <Content-Type> field, see RFC9110"


def test_fetch_review_text_matches_rewrapped_request_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page may re-wrap the request comment, so the decoy check
    compares whitespace-insensitively."""
    page = (
        '<pre class="pasted">Requested for\n   issue 450.</pre>'
        '<pre class="pasted">The actual review body.</pre>'
    )
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review("Requested for issue 450.")) == (
        "The actual review body."
    )


def test_fetch_review_text_none_when_no_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch, {_REVIEW_DOC: "<html>no review here</html>"})
    assert fetch_review_text(_review()) is None


def test_fetch_review_text_none_when_only_request_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page whose only block is the request note has no review text —
    better to skip than to file the requester's words as the review."""
    page = '<pre class="pasted">Requested for issue 450.</pre>'
    _stub_page(monkeypatch, {_REVIEW_DOC: page})
    assert fetch_review_text(_review("Requested for issue 450.")) is None


def test_fetch_review_text_none_when_page_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_page(monkeypatch, {})
    assert fetch_review_text(_review()) is None


# --- rendering / writing --------------------------------------------------


def test_render_review_has_header_and_body() -> None:
    review = _review("Requested for issue 450.")
    review.text = "  Body text.\n"
    out = render_review(review)

    assert out.startswith("# Early review of draft-ietf-ppm-dap-09")
    assert "**Reviewer:** Mark Nottingham" in out
    assert "**Team:** httpdir" in out
    assert "**Result:** Ready with Issues" in out
    assert "Requested for issue 450." in out
    assert out.rstrip().endswith("Body text.")
    # Date only — the API's full timestamp is noise in the corpus.
    assert "**Completed:** 2023-12-29  " in out
    assert "08:57:44" not in out


def test_render_review_falls_back_to_raw_slugs() -> None:
    """An unknown result / type slug renders as itself rather than
    vanishing — Datatracker can add names without a code change here."""
    review = _review()
    review.result = "brand-new-result"
    review.review_type = "brand-new-type"
    review.text = "Body."
    out = render_review(review)

    assert "**Result:** brand-new-result" in out
    assert out.startswith("# brand-new-type review of")


def test_render_review_omits_missing_optional_fields() -> None:
    review = _review()
    review.team = ""
    review.result = ""
    review.completed_on = ""
    review.text = "Body."
    out = render_review(review)

    assert "**Team:**" not in out
    assert "**Result:**" not in out
    assert "**Completed:**" not in out


def test_render_review_uses_bare_doc_name_without_revision() -> None:
    review = _review()
    review.reviewed_rev = ""
    review.text = "Body."
    assert "# Early review of draft-ietf-ppm-dap\n" in render_review(review)


def test_write_review_files(tmp_path: Path) -> None:
    review = _review()
    review.text = "Body."
    written = write_review_files(str(tmp_path), [review], Q)

    assert len(written) == 1
    path = tmp_path / "reviews" / f"{_REVIEW_DOC}.md"
    assert path.is_file()
    assert "Body." in path.read_text(encoding="utf-8")


def test_write_review_files_no_reviews_makes_no_dir(tmp_path: Path) -> None:
    assert write_review_files(str(tmp_path), [], Q) == []
    assert not (tmp_path / "reviews").exists()
