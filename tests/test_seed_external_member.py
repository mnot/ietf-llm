"""Externally-sourced seed members (#230).

The RFC corpus is assembled from an upstream artifact rather than gathered,
which changes three things in the publisher: how it is refreshed, what its
version means, and what goes in its bundle. Each is asserted here; the
upstream build itself is stubbed, since fetching a 138 MB release is not
what these are about.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ietf_llm.seed import format as fmt
from ietf_llm.seed.publish import (
    SOURCE_GATHER,
    SOURCE_RFC_INDEX,
    MemberSpec,
    _source_for,
    load_members,
    save_members,
)


def test_the_rfc_corpus_implies_its_source() -> None:
    """Implied from the name rather than a flag: there is exactly one way to
    build it, so making an operator say so is a way to get it wrong."""
    assert _source_for("rfcs") == SOURCE_RFC_INDEX
    assert _source_for("httpbis") == SOURCE_GATHER


def test_source_round_trips_through_membership(tmp_path: Any) -> None:
    store = str(tmp_path / "store")
    save_members(
        store,
        {
            "httpbis": MemberSpec(window_months=12),
            "rfcs": MemberSpec(source=SOURCE_RFC_INDEX),
        },
    )
    back = load_members(store)
    assert back["rfcs"].source == SOURCE_RFC_INDEX
    assert back["rfcs"].externally_sourced
    assert not back["httpbis"].externally_sourced


def test_a_spec_written_before_source_existed_reads_as_gathered(
    tmp_path: Any,
) -> None:
    """Existing stores have no `source` key; they are all gathered members."""
    store = str(tmp_path / "store")
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "members.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"format": fmt.FORMAT_VERSION, "members": {"tls": {"window_months": 6}}},
            fh,
        )
    spec = load_members(store)["tls"]
    assert spec.source == SOURCE_GATHER
    assert spec.window_months == 6


def test_an_index_only_bundle_carries_no_files_tree(tmp_path: Any) -> None:
    """This corpus *is* an index — the text lives in it, and there is no
    gathered tree to ship beside it."""
    corpus_dir = tmp_path / "corpus"
    index_dir = tmp_path / "index"
    (corpus_dir / "files").mkdir(parents=True)
    (corpus_dir / "files" / "draft.txt").write_text("should not ship", encoding="utf-8")
    index_dir.mkdir(parents=True)
    (index_dir / "embeddings.db").write_text("db", encoding="utf-8")

    arcs = [a for a, _p in fmt.iter_index_members(str(index_dir))]
    assert arcs == ["embeddings.db"]

    # …where the gathered form does ship it, so the difference is real.
    gathered = [a for a, _p in fmt.iter_bundle_members(str(corpus_dir), str(index_dir))]
    assert "files/draft.txt" in gathered


def test_index_only_bundle_skips_absent_optional_files(tmp_path: Any) -> None:
    """`topics.json` is not built for this corpus; its absence is not an error."""
    index_dir = tmp_path / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "embeddings.db").write_text("db", encoding="utf-8")
    assert [a for a, _p in fmt.iter_index_members(str(index_dir))] == ["embeddings.db"]


def test_a_dry_run_reports_the_external_member_as_publishable(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has no local `embeddings.db` until the run builds one, so reading a
    compatibility tuple for it during a dry run reports a skip for exactly the
    thing the run would do. An operator uses the dry run to decide; it must
    not describe the member as skipped when it would succeed.
    """
    from ietf_llm.seed.publish import publish_store

    store = str(tmp_path / "store")
    report = publish_store(
        store, add=["rfcs"], dry_run=True, gather=lambda n, m: None
    )
    assert [name for name, _v, _b in report.published] == ["rfcs"]
    assert report.skipped == []
