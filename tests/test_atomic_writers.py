"""Atomic-write fallout (issue #287, the `.tmp` item of #308): the identity
caches go through `atomic_open`, and a leaked `atomic_open` temp is invisible to
every lister, scanner and publish walk."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

import pytest

from ietf_llm.atomicio import atomic_open
from ietf_llm.gather.sources import identity_cache
from ietf_llm.mcp import corpus as mcp_corpus
from ietf_llm.mcp.grep import tool_grep_corpus
from ietf_llm.paths import get_wg_file_cache_dir
from ietf_llm.seed.format import iter_bundle_members
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore

from conftest import write_cache_file


def _leaked_temp(directory: Path, name: str) -> str:
    """Leave behind the temp a killed `atomic_open` writer would (SIGTERM skips
    the cleanup), and return its name. The name is captured from inside a real
    writer so it tracks the production format."""
    target = directory / name
    captured: List[str] = []
    with atomic_open(str(target)) as fh:
        fh.write("PARTIAL-SENTINEL-TEXT")
        fh.flush()
        captured = [n for n in os.listdir(directory) if n != name]
        assert len(captured) == 1
        (directory / "keep-copy").write_text((directory / captured[0]).read_text())
    # The writer finished and renamed its temp away; recreate it as a leftover.
    leaked = directory / captured[0]
    leaked.write_text((directory / "keep-copy").read_text())
    (directory / "keep-copy").unlink()
    return captured[0]


# --- identity caches --------------------------------------------------------


def test_identity_cache_save_roundtrip_leaves_no_temp(tmp_path: Path) -> None:
    path = str(tmp_path / "sub" / "_github-users.json")
    identity_cache.save(path, {"alice": {"name": "Alice"}})
    assert json.loads(Path(path).read_text()) == {"alice": {"name": "Alice"}}
    assert os.listdir(tmp_path / "sub") == ["_github-users.json"]


def test_identity_cache_save_uses_distinct_temp_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two gather processes saving the same cross-corpus file must not share a
    # temp: the old fixed `path + ".tmp"` let one truncate the other's.
    path = str(tmp_path / "_datatracker-people.json")
    seen: List[str] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        seen.append(src)
        real_replace(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.replace", spy)
    identity_cache.save(path, {"a": {}})
    identity_cache.save(path, {"b": {}})
    assert len(set(seen)) == 2


def test_identity_cache_save_failure_keeps_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "_github-users.json"
    identity_cache.save(str(path), {"old": {}})

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ietf_llm.atomicio.os.replace", boom)
    identity_cache.save(str(path), {"new": {}})  # best-effort: must not raise
    assert json.loads(path.read_text()) == {"old": {}}
    assert os.listdir(tmp_path) == ["_github-users.json"]


# --- a leaked temp is invisible ---------------------------------------------


def test_list_files_and_grep_ignore_tmp_fragment(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-04-10-real.md", "REAL body\n")
    threads = Path(get_wg_file_cache_dir("wg")) / "threads"
    leaked = _leaked_temp(threads, "2026-04-10-real.md")
    assert (threads / leaked).read_text() == "PARTIAL-SENTINEL-TEXT"

    listing = mcp_corpus.tool_list_files("wg")
    assert "2026-04-10-real.md" in listing
    assert leaked not in listing

    hits = tool_grep_corpus("wg", "PARTIAL-SENTINEL-TEXT")
    assert leaked not in hits
    assert "REAL" not in hits  # the pattern occurs only in the temp


def test_publish_walk_and_seed_bundle_skip_tmp_fragment(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    threads = ws / "files" / "threads"
    threads.mkdir(parents=True)
    (threads / "a.md").write_text("A")
    leaked = _leaked_temp(threads, "a.md")

    store = CloudCorpusStore(
        KvControlPlane(InMemoryKvStore()),
        FileBlobStore(str(tmp_path / "bucket")),
        str(tmp_path / "scratch"),
    )
    store.publish("tls", str(ws), version="v1")
    assert not list((tmp_path / "bucket").rglob(leaked))
    assert list((tmp_path / "bucket").rglob("a.md"))

    members = [arc for arc, _path in iter_bundle_members(str(ws), str(tmp_path / "idx"))]
    assert "files/threads/a.md" in members
    assert not [m for m in members if m.endswith(".tmp")]
