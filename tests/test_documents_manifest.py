"""Tests for the per-draft expiry manifest: round-trip, the expiry
capture in get_wg_documents, and the overview active/concluded split.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ietf_llm.gather import drafts as drafts_mod
from ietf_llm.gather.documents_manifest import (
    load_documents_manifest,
    save_documents_manifest,
)
from ietf_llm.utils import Verbosity


# --- manifest round-trip --------------------------------------------------


def test_manifest_roundtrip(isolated_home: Path) -> None:
    assert load_documents_manifest("httpbis") == {}  # absent → empty
    save_documents_manifest("httpbis", {"draft-ietf-httpbis-x": "2026-11-14T00:00:00Z"})
    assert load_documents_manifest("httpbis") == {
        "draft-ietf-httpbis-x": "2026-11-14T00:00:00Z"
    }


def test_manifest_corrupt_returns_empty(isolated_home: Path) -> None:
    from ietf_llm.utils import get_cache_dir

    path = os.path.join(get_cache_dir(), "httpbis", "documents.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert load_documents_manifest("httpbis") == {}


# --- get_wg_documents captures expiry -------------------------------------


def test_get_wg_documents_captures_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drafts_mod, "get_group_type", lambda wg: "ietf")

    def fake_iter(wg, doc_type):
        if doc_type == "draft":
            return iter(
                [
                    {
                        "name": "draft-ietf-httpbis-live",
                        "rev": "03",
                        "expires": "2026-11-14T00:00:00Z",
                    },
                    {  # no expires field → omitted from the manifest
                        "name": "draft-ietf-httpbis-nodate",
                        "rev": "00",
                    },
                ]
            )
        return iter([])

    monkeypatch.setattr(drafts_mod, "iter_group_documents", fake_iter)
    docs = drafts_mod.get_wg_documents("httpbis", Verbosity.QUIET)
    by_name = {d["name"]: d for d in docs["drafts"]}
    assert by_name["draft-ietf-httpbis-live"]["expires"] == "2026-11-14T00:00:00Z"
    assert by_name["draft-ietf-httpbis-nodate"]["expires"] == ""
