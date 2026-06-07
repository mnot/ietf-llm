"""Tests for the CorpusStore seam and its local (filesystem) backend."""

from __future__ import annotations

from pathlib import Path

from ietf_llm.corpus_store import (
    LOCAL_VERSION,
    CorpusStore,
    LocalCorpusStore,
    get_corpus_store,
)

from conftest import write_cache_file


def test_list_corpora_finds_cached_sorted(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    assert LocalCorpusStore().list_corpora() == ["httpbis", "tls"]


def test_list_corpora_skips_dot_and_underscore_dirs(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "tls", "digests/index.md", "# x\n")
    write_cache_file(isolated_home, "_scratch", "digests/index.md", "# x\n")
    assert LocalCorpusStore().list_corpora() == ["tls"]


def test_resolve_current_present_vs_absent(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    store = LocalCorpusStore()
    assert store.resolve_current("httpbis") == LOCAL_VERSION
    assert store.resolve_current("ghost") is None


def test_corpus_exists(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    store = LocalCorpusStore()
    assert store.corpus_exists("httpbis") is True
    assert store.corpus_exists("ghost") is False


def test_local_cache_dir_present_vs_absent(isolated_home: Path) -> None:
    write_cache_file(isolated_home, "httpbis", "digests/index.md", "# x\n")
    store = LocalCorpusStore()
    expected = isolated_home / ".cache" / "ietf-llm" / "httpbis" / "files"
    assert store.local_cache_dir("httpbis") == str(expected)
    assert store.local_cache_dir("ghost") is None


def test_absent_corpus_is_never_materialised(isolated_home: Path) -> None:
    # The typo-safety invariant: probing a corpus that does not exist must
    # not create its cache dir (unlike utils.get_wg_file_cache_dir, which does).
    store = LocalCorpusStore()
    store.corpus_exists("ghost")
    store.resolve_current("ghost")
    store.local_cache_dir("ghost")
    assert not (isolated_home / ".cache" / "ietf-llm" / "ghost").exists()


def test_seed_workspace_is_noop_on_local(isolated_home: Path) -> None:
    # The local cache already *is* the workspace, so seeding is a no-op that
    # returns None and creates nothing.
    store = LocalCorpusStore()
    dest = isolated_home / ".cache" / "ietf-llm" / "tls"
    assert store.seed_workspace("tls", str(dest)) is None
    assert not dest.exists()


def test_get_corpus_store_returns_local_backend() -> None:
    store = get_corpus_store()
    assert isinstance(store, CorpusStore)
    assert isinstance(store, LocalCorpusStore)
