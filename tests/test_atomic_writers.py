"""Atomic-write fallout (issue #287, the `.tmp` item of #308): the identity
caches go through `atomic_open`, and a leaked `atomic_open` temp is invisible to
every lister, scanner and publish walk."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List

import pytest

from ietf_llm.atomicio import STALE_TMP_SECONDS, _atomic_tmp_path, atomic_open
from ietf_llm.gather import sequencer
from ietf_llm.gather.sources import _mirror, identity_cache, pdf_extract
from ietf_llm.gather.sources.github import process_github_issues
from ietf_llm.gather.sources.pdf_extract import extract_all_pdfs
from ietf_llm.log import Verbosity
from ietf_llm.mcp import corpus as mcp_corpus
from ietf_llm.mcp.grep import tool_grep_corpus
from ietf_llm.paths import get_cache_dir, get_wg_file_cache_dir
from ietf_llm.seed.format import iter_bundle_members
from ietf_llm.store.blobs import FileBlobStore
from ietf_llm.store.cloud import CloudCorpusStore
from ietf_llm.store.control import KvControlPlane
from ietf_llm.store.kv import InMemoryKvStore

from conftest import make_issue, write_cache_file, write_github_archive
from test_pdf_extract import MINIMAL_PDF


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


# --- round 2: mirrors, PDF text, issue text ---------------------------------


def _fail_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ietf_llm.atomicio.os.replace", boom)


def test_mirror_write_body_failure_keeps_old_body_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rfc-index.json"
    assert _mirror.write_body(str(path), b"old", Verbosity.QUIET, "RFC index")
    _fail_replace(monkeypatch)
    assert not _mirror.write_body(str(path), b"new", Verbosity.QUIET, "RFC index")
    assert path.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["rfc-index.json"]


def test_mirror_write_sidecar_failure_keeps_old_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    etag = tmp_path / "rfc-index.json.etag"
    _mirror.write_sidecar(str(etag), '"v1"')
    _fail_replace(monkeypatch)
    _mirror.write_sidecar(str(etag), '"v2"')  # best-effort: must not raise
    assert etag.read_text() == '"v1"'
    assert os.listdir(tmp_path) == ["rfc-index.json.etag"]


def test_mirror_writes_use_the_dot_prefixed_atomic_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[str] = []
    real_replace = os.replace

    def spy(src: str, dst: str) -> None:
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr("ietf_llm.atomicio.os.replace", spy)
    _mirror.write_body(str(tmp_path / "body"), b"x", Verbosity.QUIET, "t")
    _mirror.write_sidecar(str(tmp_path / "body.etag"), '"e"')
    assert len(seen) == 2
    assert all(name.startswith(".") and name.endswith(".tmp") for name in seen)


def _slide(tmp_path: Path) -> Path:
    pdf = tmp_path / "slides.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    return pdf


def test_pdf_extract_one_failing_mid_write_keeps_old_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _slide(tmp_path)
    txt = tmp_path / "slides.pdf.txt"
    txt.write_text("OLD TEXT")
    # A lone surrogate fails the UTF-8 encode part-way through the write.
    monkeypatch.setattr(pdf_extract, "extract_pdf_text", lambda _p: "a \ud800 b\n")
    with pytest.raises(UnicodeEncodeError):
        pdf_extract._extract_one(
            str(pdf), "slides.pdf", str(txt), str(tmp_path)
        )
    assert txt.read_text() == "OLD TEXT"
    assert sorted(os.listdir(tmp_path)) == ["slides.pdf", "slides.pdf.txt"]


def test_pdf_failed_reextraction_leaves_no_stale_txt_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slide(tmp_path)
    txt = tmp_path / "slides.pdf.txt"
    txt.write_text("OLD DECK BODY")
    pdf = tmp_path / "slides.pdf"
    os.utime(pdf, (time.time() + 60, time.time() + 60))  # the deck was replaced
    monkeypatch.setattr(pdf_extract, "extract_pdf_text", lambda _p: "a \ud800 b\n")
    assert extract_all_pdfs(str(tmp_path)) == []
    # The previous deck's text must not stay searchable under the new .pdf, nor
    # survive to be "refreshed" into looking current (a header rewrite bumps mtime).
    assert not txt.exists()
    assert os.listdir(tmp_path) == ["slides.pdf"]
    monkeypatch.undo()
    assert extract_all_pdfs(str(tmp_path)) == [str(txt)]
    assert "Hello AIPREF" in txt.read_text()


def test_pdf_stale_txt_whose_header_is_rewritten_is_still_reextracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An mtime bump by any later rewrite of a stale .txt (the PR #324 header
    refresh does this through `write_if_changed`) must not make it look
    current: the failed extraction has already removed it."""
    pdf = _slide(tmp_path)
    txt = tmp_path / "slides.pdf.txt"
    txt.write_text("OLD DECK BODY")
    os.utime(pdf, (time.time() + 60, time.time() + 60))
    monkeypatch.setattr(pdf_extract, "extract_pdf_text", lambda _p: "a \ud800 b\n")
    extract_all_pdfs(str(tmp_path))
    assert not txt.exists()  # nothing left for a rewrite to bump
    assert pdf_extract._needs_extraction(
        str(pdf), str(txt)
    )


def test_pdf_zero_length_txt_is_reextracted(tmp_path: Path) -> None:
    _slide(tmp_path)
    txt = tmp_path / "slides.pdf.txt"
    txt.write_text("")
    # Newer than the .pdf, so the mtime check alone would trust it.
    os.utime(txt, (time.time() + 60, time.time() + 60))
    assert extract_all_pdfs(str(tmp_path)) == [str(txt)]
    assert "Hello AIPREF" in txt.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_pdf_unreadable_txt_is_reextracted(tmp_path: Path) -> None:
    _slide(tmp_path)
    txt = tmp_path / "slides.pdf.txt"
    txt.write_text("GARBAGE")
    os.utime(txt, (time.time() + 60, time.time() + 60))
    txt.chmod(0)
    assert extract_all_pdfs(str(tmp_path)) == [str(txt)]
    assert "Hello AIPREF" in txt.read_text()


def test_pdf_needs_extraction_when_txt_cannot_be_statted(tmp_path: Path) -> None:
    pdf = _slide(tmp_path)
    dangling = tmp_path / "slides.pdf.txt"
    os.symlink(tmp_path / "nowhere", dangling)
    assert pdf_extract._needs_extraction(
        str(pdf), str(dangling)
    )


class _ExplodingRegistry:
    def canonical_for_github(self, login: str) -> str:
        raise RuntimeError("registry blew up")


def test_issues_txt_failing_mid_write_keeps_old_file(
    isolated_home: Path, tmp_path: Path
) -> None:
    archive = write_github_archive(
        isolated_home, "wg", "org/repo", [make_issue(1, "Title", author="mnot")]
    )
    outdir = tmp_path / "raw"
    outdir.mkdir()
    out = outdir / "out.txt"
    out.write_text("OLD ISSUES TEXT")
    with pytest.raises(RuntimeError):
        process_github_issues(str(archive), str(out), registry=_ExplodingRegistry())
    assert out.read_text() == "OLD ISSUES TEXT"
    assert os.listdir(outdir) == ["out.txt"]


# --- the temp sweep ---------------------------------------------------------


def _leaked_path(directory: Path, name: str) -> Path:
    """A leaked temp beside a file called `name` in `directory`."""
    temp = Path(_atomic_tmp_path(str(directory / name)))
    temp.write_text("PARTIAL")
    return temp


def _old(path: Path, age: float = 2 * STALE_TMP_SECONDS) -> Path:
    past = time.time() - age
    os.utime(path, (past, past))
    return path


def test_gather_sweep_reaps_old_temps_in_every_dir_a_gather_writes(
    isolated_home: Path,
) -> None:
    root = Path(get_cache_dir())
    dirs = [
        root / "wg" / "files" / "threads",
        root / "wg",
        root / "imap-cache" / "wg" / "list",
        root,
        root / "_rfc",
        root / "_catalog",
        root / "_groups" / "files" / "state",
    ]
    old: List[Path] = []
    fresh: List[Path] = []
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
        old.append(_old(_leaked_path(directory, "stale")))
        fresh.append(_leaked_path(directory, "recent"))
    other = root / "other" / "files"  # another corpus: not this gather's to touch
    other.mkdir(parents=True)
    foreign = _old(_leaked_path(other, "foreign"))
    sequencer._sweep_leaked_temps("wg", Verbosity.QUIET)
    assert not [p for p in old if p.exists()]
    assert all(p.exists() for p in fresh)
    assert foreign.exists()


def test_gather_sweep_failure_is_never_fatal(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(sequencer, "sweep_stale_tmp", boom)
    sequencer._sweep_leaked_temps("wg", Verbosity.QUIET)


def test_gather_one_sweeps_after_seeding(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ietf_llm.gather.cli import build_parser

    class _Stop(Exception):
        pass

    calls: List[str] = []

    def sweep(wg: str, _verbosity: Verbosity) -> None:
        calls.append(f"sweep:{wg}")
        raise _Stop

    monkeypatch.setattr(sequencer, "_maybe_seed", lambda *a, **k: calls.append("seed"))
    monkeypatch.setattr(sequencer, "_resolve_corpus_shape", lambda *a, **k: (False, True))
    monkeypatch.setattr(sequencer, "_sweep_leaked_temps", sweep)
    with pytest.raises(_Stop):
        sequencer._gather_one(
            build_parser().parse_args(["tls"]), Verbosity.QUIET
        )
    assert calls == ["seed", "sweep:tls"]


def test_the_sweep_is_only_reachable_from_the_gather_path() -> None:
    import ietf_llm

    package = Path(ietf_llm.__file__).parent
    users = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "sweep_stale_tmp" in path.read_text(encoding="utf-8")
    )
    assert users == ["atomicio.py", os.path.join("gather", "sequencer.py")]


def test_read_tools_never_delete_a_leaked_temp(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_cache_file(isolated_home, "wg", "threads/2026-04-10-real.md", "REAL body\n")
    threads = Path(get_wg_file_cache_dir("wg")) / "threads"
    leaked = _old(_leaked_path(threads, "2026-04-10-real.md"))

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a read tool swept the cache")

    monkeypatch.setattr("ietf_llm.atomicio.sweep_stale_tmp", boom)
    monkeypatch.setattr(sequencer, "sweep_stale_tmp", boom)
    mcp_corpus.tool_list_files("wg")
    tool_grep_corpus("wg", "REAL")
    assert leaked.exists()
