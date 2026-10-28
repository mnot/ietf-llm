"""Tests for materials rev-gating: the manifest round-trip and the
`_needs_rebuild` freshness decision that drives minutes/agenda
re-fetching.
"""

from __future__ import annotations

import os
from pathlib import Path

from ietf_llm.gather.sources.materials_manifest import load_manifest, save_manifest
from ietf_llm.gather.sources.meetings import (
    _AGGREGATE_FORMAT,
    _format_key,
    _needs_rebuild,
)


# --- manifest round-trip --------------------------------------------------


def test_manifest_roundtrip(isolated_home: Path) -> None:
    assert load_manifest("httpbis") == {}  # absent → empty
    save_manifest("httpbis", {"minutes-125-httpbis": "01"})
    assert load_manifest("httpbis") == {"minutes-125-httpbis": "01"}


def test_manifest_corrupt_returns_empty(isolated_home: Path) -> None:
    from ietf_llm.paths import get_cache_dir

    path = os.path.join(get_cache_dir(), "httpbis", "materials.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert load_manifest("httpbis") == {}


# --- _needs_rebuild -------------------------------------------------------


def _link(docname: str) -> dict:
    return {"type": "minutes", "url": "x", "docname": docname}


_KEY = _format_key("ietf125", "minutes")
_STAMP = {_KEY: _AGGREGATE_FORMAT}


def test_needs_rebuild_when_file_missing(tmp_path: Path) -> None:
    out = str(tmp_path / "minutes.md")
    assert _needs_rebuild(out, [_link("minutes-125-httpbis")], {}, {}, _KEY) is True


def test_needs_rebuild_false_when_revs_match(tmp_path: Path) -> None:
    out = tmp_path / "minutes.md"
    out.write_text("# minutes\n")
    revs = {"minutes-125-httpbis": "01"}
    manifest = {"minutes-125-httpbis": "01", **_STAMP}
    assert _needs_rebuild(str(out), [_link("minutes-125-httpbis")], revs, manifest, _KEY) is False


def test_needs_rebuild_true_when_rev_changed(tmp_path: Path) -> None:
    out = tmp_path / "minutes.md"
    out.write_text("# minutes\n")
    revs = {"minutes-125-httpbis": "02"}        # upstream revised
    manifest = {"minutes-125-httpbis": "01", **_STAMP}    # we last wrote 01
    assert _needs_rebuild(str(out), [_link("minutes-125-httpbis")], revs, manifest, _KEY) is True


def test_needs_rebuild_unknown_rev_does_not_thrash(tmp_path: Path) -> None:
    # A doc with no known current rev (not in revs) must not force a
    # perpetual rebuild once the file exists.
    out = tmp_path / "minutes.md"
    out.write_text("# minutes\n")
    assert _needs_rebuild(str(out), [_link("minutes-xx-joint")], {}, dict(_STAMP), _KEY) is False


def test_needs_rebuild_when_format_marker_missing_or_old(tmp_path: Path) -> None:
    # An aggregate written under an older layout is regenerated even though
    # every revision matches.
    out = tmp_path / "minutes.md"
    out.write_text("# minutes\n")
    revs = {"minutes-125-httpbis": "01"}
    links = [_link("minutes-125-httpbis")]
    assert _needs_rebuild(str(out), links, revs, {"minutes-125-httpbis": "01"}, _KEY) is True
    old = {"minutes-125-httpbis": "01", _KEY: "1"}
    assert _needs_rebuild(str(out), links, revs, old, _KEY) is True
