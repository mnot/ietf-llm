"""Tests for the per-draft expiry manifest: round-trip, the expiry
capture in get_wg_documents, and the overview active/concluded split.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ietf_llm.gather.sources import drafts as drafts_mod
from ietf_llm.gather.sources.documents_manifest import (
    load_documents_manifest,
    save_documents_manifest,
)
from ietf_llm.utils import Verbosity


# --- manifest round-trip --------------------------------------------------


def test_manifest_roundtrip(isolated_home: Path) -> None:
    assert load_documents_manifest("httpbis") == {}  # absent → empty
    save_documents_manifest(
        "httpbis",
        {"draft-ietf-httpbis-x": {"expires": "2026-11-14T00:00:00Z", "state": "active"}},
    )
    assert load_documents_manifest("httpbis") == {
        "draft-ietf-httpbis-x": {"expires": "2026-11-14T00:00:00Z", "state": "active"}
    }


def test_manifest_normalises_legacy_flat_shape(isolated_home: Path) -> None:
    """A manifest written before state was recorded mapped name → expiry
    string. The loader lifts that into the record shape so a stale cache
    keeps working until its next gather rewrites it."""
    import json

    from ietf_llm.utils import get_cache_dir

    path = os.path.join(get_cache_dir(), "httpbis", "documents.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"draft-ietf-httpbis-old": "2020-01-01T00:00:00Z"}, fh)
    assert load_documents_manifest("httpbis") == {
        "draft-ietf-httpbis-old": {"expires": "2020-01-01T00:00:00Z", "state": None}
    }


def test_skip_embed_draft_names(isolated_home: Path) -> None:
    """Only drafts whose state is in SKIP_EMBED_STATES (rfc / repl) are
    returned; active / expired stay embeddable, as do unknown-state
    drafts."""
    from ietf_llm.gather.sources.documents_manifest import skip_embed_draft_names

    save_documents_manifest(
        "httpbis",
        {
            "draft-ietf-httpbis-semantics": {"expires": "", "state": "rfc"},
            "draft-ietf-httpbis-p2-semantics": {"expires": "", "state": "repl"},
            "draft-ietf-httpbis-live": {"expires": "2099-01-01T00:00:00Z", "state": "active"},
            "draft-ietf-httpbis-stale": {"expires": "2020-01-01T00:00:00Z", "state": "expired"},
            "draft-ietf-httpbis-mystery": {"expires": "", "state": None},
        },
    )
    assert skip_embed_draft_names("httpbis") == {
        "draft-ietf-httpbis-semantics",
        "draft-ietf-httpbis-p2-semantics",
    }
    assert skip_embed_draft_names("nonexistent-wg") == set()


def test_manifest_corrupt_returns_empty(isolated_home: Path) -> None:
    from ietf_llm.utils import get_cache_dir

    path = os.path.join(get_cache_dir(), "httpbis", "documents.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert load_documents_manifest("httpbis") == {}


# --- get_wg_documents captures expiry -------------------------------------


def test_get_wg_documents_captures_expiry_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafts_mod, "get_group_type", lambda wg: "ietf")
    # Draft-state URI→slug map: the document objects carry these URIs in
    # their `states` list, and get_wg_documents resolves the draft-type one.
    monkeypatch.setattr(
        drafts_mod,
        "draft_state_slugs",
        lambda: {"/api/v1/doc/state/1/": "active", "/api/v1/doc/state/3/": "rfc"},
    )

    def fake_iter(wg, doc_type):
        if doc_type == "draft":
            return iter(
                [
                    {
                        "name": "draft-ietf-httpbis-live",
                        "rev": "03",
                        "expires": "2026-11-14T00:00:00Z",
                        # mixes a draft-type state URI with an unrelated one
                        "states": ["/api/v1/doc/state/44/", "/api/v1/doc/state/1/"],
                    },
                    {  # no expires field → "" in the draft dict; state resolved
                        "name": "draft-ietf-httpbis-nodate",
                        "rev": "00",
                        "states": ["/api/v1/doc/state/3/"],
                    },
                ]
            )
        return iter([])

    monkeypatch.setattr(drafts_mod, "iter_group_documents", fake_iter)
    docs = drafts_mod.get_wg_documents("httpbis", Verbosity.QUIET)
    by_name = {d["name"]: d for d in docs["drafts"]}
    assert by_name["draft-ietf-httpbis-live"]["expires"] == "2026-11-14T00:00:00Z"
    assert by_name["draft-ietf-httpbis-live"]["state"] == "active"
    assert by_name["draft-ietf-httpbis-nodate"]["expires"] == ""
    assert by_name["draft-ietf-httpbis-nodate"]["state"] == "rfc"


# --- include_related merges & filters --------------------------------------


def test_get_wg_documents_include_related(monkeypatch: pytest.MonkeyPatch) -> None:
    """`include_related=True` should pull in `draft-<author>-<wg>-<topic>`
    and drop names where <wg> isn't in slug position 2 (i.e. another WG's
    adoption, or an ill-formed name)."""
    monkeypatch.setattr(drafts_mod, "get_group_type", lambda wg: "ietf")

    def fake_iter(wg: str, doc_type: str) -> Any:
        return iter([])

    def fake_iter_related(wg: str) -> Any:
        return iter(
            [
                {  # kept — position 2 is `oauth`
                    "name": "draft-aap-oauth-profile",
                    "rev": "01",
                    "expires": "2026-12-01T00:00:00Z",
                },
                {  # dropped — adopted by mailmaint, position 2 is `mailmaint`
                    "name": "draft-ietf-mailmaint-oauth-public",
                    "rev": "00",
                },
                {  # dropped — only 3 slugs, no <topic> after <wg>
                    "name": "draft-oauth-foo",
                    "rev": "00",
                },
                {  # kept — picks max rev
                    "name": "draft-parecki-oauth-jwt-dpop-grant",
                    "rev": "03",
                },
                {  # superseded by the rev 03 above
                    "name": "draft-parecki-oauth-jwt-dpop-grant",
                    "rev": "02",
                },
            ]
        )

    monkeypatch.setattr(drafts_mod, "iter_group_documents", fake_iter)
    monkeypatch.setattr(drafts_mod, "iter_active_drafts_by_name", fake_iter_related)

    docs = drafts_mod.get_wg_documents(
        "oauth", Verbosity.QUIET, include_related=True
    )
    by_name = {d["name"]: d for d in docs["drafts"]}
    assert set(by_name) == {
        "draft-aap-oauth-profile",
        "draft-parecki-oauth-jwt-dpop-grant",
    }
    assert by_name["draft-aap-oauth-profile"]["expires"] == "2026-12-01T00:00:00Z"
    assert by_name["draft-parecki-oauth-jwt-dpop-grant"]["max_rev"] == 3


def test_get_wg_documents_include_related_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the flag, the related-drafts iterator is never consulted."""
    monkeypatch.setattr(drafts_mod, "get_group_type", lambda wg: "ietf")
    monkeypatch.setattr(drafts_mod, "iter_group_documents", lambda *a, **k: iter([]))

    def boom(wg: str) -> Any:
        raise AssertionError("iter_active_drafts_by_name should not be called")

    monkeypatch.setattr(drafts_mod, "iter_active_drafts_by_name", boom)
    drafts_mod.get_wg_documents("oauth", Verbosity.QUIET)
