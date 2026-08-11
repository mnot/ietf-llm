"""Tests for the seed-store consumer fetch/install (`ietf_llm.seed.fetch`) and
the `seed_url` service knob.

The core is a publisher -> consumer round-trip: publish a corpus to a store dir,
wipe it from the cache, then install it back from the store and confirm a usable
corpus tree reappears. Covers best-effort index load, install (cold + over an
existing tree), split-index relocation, tamper detection, provenance, and the
opt-out disable semantics of seed_url().
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from ietf_llm import freshness
from ietf_llm.embeddings.storage import _SCHEMA_VERSION
from ietf_llm.config import service
from ietf_llm.paths import get_cache_dir, get_index_dir
from ietf_llm.seed import fetch
from ietf_llm.seed import format as fmt
from ietf_llm.seed import publish


def _gathered(name, *, model="sentence-transformers/BAAI/bge-small-en-v1.5"):
    files = os.path.join(get_cache_dir(), name, "files")
    os.makedirs(files, exist_ok=True)
    with open(os.path.join(files, "charter.txt"), "w") as fh:
        fh.write(f"charter for {name}")
    # An incremental-gather manifest belongs in the bundle so a follow-on gather
    # is incremental, not cold.
    with open(os.path.join(get_cache_dir(), name, "documents.json"), "w") as fh:
        fh.write("{}")
    db = os.path.join(get_index_dir(), name, "embeddings.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("model", model), ("schema_version", str(_SCHEMA_VERSION)),
        ("chunker_version", "2"), ("embed_dim", "384")])
    conn.commit()
    conn.close()
    freshness.record_gather(name)


def _publish_store(store, name="httpbis"):
    _gathered(name)
    publish.publish_store(store, add=[name], no_gather=True,
                          gather=lambda n, m: None)
    return fmt.Index.from_json(open(os.path.join(store, "index.json")).read())


def test_load_index_best_effort(isolated_home, tmp_path):
    # Nonexistent base -> None, not an error.
    assert fetch.load_index(str(tmp_path / "nope")) is None
    # Malformed index -> None.
    store = tmp_path / "bad"
    store.mkdir()
    (store / "index.json").write_text("{not json")
    assert fetch.load_index(str(store)) is None


def test_roundtrip_install(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    idx = _publish_store(store)
    entry = idx.entry("httpbis")
    # Wipe the corpus from the cache to simulate a cold consumer.
    import shutil
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    version = fetch.install(store, entry)
    assert version == entry.version
    corpus = os.path.join(get_cache_dir(), "httpbis")
    assert open(os.path.join(corpus, "files", "charter.txt")).read() == "charter for httpbis"
    assert os.path.isfile(os.path.join(corpus, "embeddings.db"))
    # Incremental-usability: the installed manifests + last-gathered read back, so
    # a follow-on gather sees a non-None prior and goes incremental, not cold.
    assert os.path.isfile(os.path.join(corpus, "documents.json"))
    assert freshness.last_gathered("httpbis") is not None
    src = fetch.seed_source("httpbis")
    assert src["version"] == entry.version and src["url"] == store


def test_install_via_file_url(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    idx = _publish_store(store)
    import shutil
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    fetch.install("file://" + store, idx.entry("httpbis"))
    assert os.path.isfile(
        os.path.join(get_cache_dir(), "httpbis", "files", "charter.txt"))


def test_install_replaces_existing(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    idx = _publish_store(store)
    # Leave a marker in the existing corpus dir; install should replace the tree.
    marker = os.path.join(get_cache_dir(), "httpbis", "files", "STALE")
    open(marker, "w").close()
    fetch.install(store, idx.entry("httpbis"))
    assert not os.path.exists(marker)
    assert os.path.isfile(
        os.path.join(get_cache_dir(), "httpbis", "files", "charter.txt"))


def test_install_split_index_dir(isolated_home, tmp_path, monkeypatch):
    store = str(tmp_path / "store")
    idx = _publish_store(store)
    import shutil
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    # Point the index dir elsewhere: embeddings.db must land there, not in the
    # corpus cache dir.
    index_root = tmp_path / "index"
    monkeypatch.setenv("IETF_LLM_INDEX_DIR", str(index_root))
    fetch.install(store, idx.entry("httpbis"))
    corpus = os.path.join(get_cache_dir(), "httpbis")
    assert os.path.isfile(os.path.join(corpus, "files", "charter.txt"))
    assert not os.path.exists(os.path.join(corpus, "embeddings.db"))
    assert os.path.isfile(os.path.join(str(index_root), "httpbis", "embeddings.db"))


def test_tamper_detected(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    idx = _publish_store(store)
    entry = idx.entry("httpbis")
    manifest = fetch.load_manifest(store, entry)
    with open(os.path.join(store, manifest.bundle), "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises((fetch.SeedFetchError, fmt.SeedFormatError)):
        fetch.install(store, entry)


def test_swap_dir_restores_prior_corpus_on_failure(isolated_home, tmp_path, monkeypatch):
    # If the staging->dest rename fails, the prior corpus must be put back — a
    # failed re-seed can never destroy a good corpus.
    dest = str(tmp_path / "corpus")
    os.makedirs(dest)
    open(os.path.join(dest, "GOOD"), "w").close()
    staging = str(tmp_path / "staging")
    os.makedirs(staging)
    open(os.path.join(staging, "NEW"), "w").close()
    real_rename = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the staging -> dest rename
            raise OSError("boom")
        return real_rename(src, dst)

    monkeypatch.setattr(fetch.os, "rename", flaky)
    with pytest.raises(fetch.SeedFetchError):
        fetch._swap_dir(staging, dest)
    assert os.path.isfile(os.path.join(dest, "GOOD"))  # restored
    assert not os.path.exists(os.path.join(dest, "NEW"))


def test_seed_catalog_roundtrip(isolated_home):
    from ietf_llm.seed import catalog
    assert catalog.cached_index() is None
    idx = fmt.Index(
        generated="2026-07-01T00:00:00Z",
        compat=fmt.CompatTuple(8, "m", "2", 384),
        corpora=[fmt.IndexEntry("aipref", "group", "AIPREF", 12,
                                "2026-07-01T00:00:00Z", "v",
                                "aipref/manifest.json", 1)])
    catalog.cache_index(idx)
    back = catalog.cached_index()
    assert back is not None and back.entry("aipref") is not None


def test_list_corpora_cold_start_lists_catalog(isolated_home, tmp_path, monkeypatch):
    import shutil as _sh
    from ietf_llm.mcp import corpus as mcp_corpus
    store = str(tmp_path / "store")
    _gathered("httpbis")
    # Published under the generation segment, and the client is pointed at the
    # base — the resolution a real consumer does.
    publish.publish_store(publish.generation_dir(store), add=["httpbis"],
                          no_gather=True, gather=lambda n, m: None)
    _sh.rmtree(os.path.join(get_cache_dir(), "httpbis"))  # cold client, nothing local
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    out = mcp_corpus.tool_list_corpora()
    # The live refresh populated the mirror, so the catalog shows even cold.
    assert "Available to fast-start" in out and "httpbis" in out


def test_refresh_mirror_gated_off_when_gather_disabled(
        isolated_home, tmp_path, monkeypatch):
    from ietf_llm.seed import catalog
    store = str(tmp_path / "store")
    _gathered("httpbis")
    publish.publish_store(store, add=["httpbis"], no_gather=True,
                          gather=lambda n, m: None)
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "0")  # gather off → no live fetch
    catalog.refresh_mirror()
    assert catalog.cached_index() is None


def test_refresh_mirror_swr_revalidates_when_stale(
        isolated_home, tmp_path, monkeypatch):
    from ietf_llm.paths import seed_index_cache_path
    from ietf_llm.seed import catalog
    store = str(tmp_path / "store")
    _gathered("httpbis")
    publish.publish_store(publish.generation_dir(store), add=["httpbis"],
                          no_gather=True, gather=lambda n, m: None)
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    # A stale mirror listing a different, old corpus.
    catalog.cache_index(fmt.Index(
        generated="g", compat=fmt.CompatTuple(8, "m", "2", 384),
        corpora=[fmt.IndexEntry("oldwg", "group", "", 12, "g", "v",
                                "oldwg/manifest.json", 1)]))
    old = time.time() - 4000  # older than the 1h TTL
    os.utime(seed_index_cache_path(), (old, old))
    catalog.refresh_mirror()  # present+stale → revalidate (inline under test flag)
    idx = catalog.cached_index()
    assert idx is not None and idx.entry("httpbis") is not None


def test_refresh_mirror_skips_when_fresh(isolated_home, monkeypatch):
    from ietf_llm.seed import catalog
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://unused.example/")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    catalog.cache_index(fmt.Index(
        generated="g", compat=fmt.CompatTuple(8, "m", "2", 384),
        corpora=[fmt.IndexEntry("wg", "group", "", 12, "g", "v",
                                "wg/manifest.json", 1)]))
    calls = []
    monkeypatch.setattr("ietf_llm.seed.fetch.load_index",
                        lambda *a, **k: calls.append(1))
    catalog.refresh_mirror()  # fresh mirror → no fetch
    assert not calls


def test_refresh_mirror_throttles_repeated_cold_attempts(isolated_home, monkeypatch):
    from ietf_llm.seed import catalog
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://down.example/")
    monkeypatch.setenv("IETF_LLM_ENABLE_GATHER", "1")
    calls = []
    monkeypatch.setattr("ietf_llm.seed.fetch.load_index",
                        lambda *a, **k: calls.append(1))  # returns None → "fails"
    catalog.refresh_mirror()  # cold → one attempt
    catalog.refresh_mirror()  # throttled → no second attempt
    assert len(calls) == 1


def test_freshness_seed_source_roundtrip(isolated_home):
    assert freshness.seed_source("httpbis") is None
    freshness.record_seed_source(
        "httpbis", url="https://seed/", version="v1",
        gathered="2026-07-01T00:00:00Z")
    src = freshness.seed_source("httpbis")
    assert src["url"] == "https://seed/" and src["version"] == "v1"
    assert src["gathered"] == "2026-07-01T00:00:00Z" and "fetched" in src


def test_seed_url_disable_semantics(isolated_home, monkeypatch):
    monkeypatch.delenv("IETF_LLM_SEED_URL", raising=False)
    assert service.seed_url() == service._DEFAULT_SEED_URL  # baked opt-out default
    monkeypatch.setenv("IETF_LLM_SEED_URL", "https://seed.example/")
    assert service.seed_url() == "https://seed.example/"
    for off in ("", "  ", "off", "None"):
        monkeypatch.setenv("IETF_LLM_SEED_URL", off)
        assert service.seed_url() is None


def test_seed_url_from_global(isolated_home, monkeypatch):
    monkeypatch.delenv("IETF_LLM_SEED_URL", raising=False)
    monkeypatch.setattr(
        service.fs, "load_global", lambda: {"seed_url": "https://g.example/"})
    assert service.seed_url() == "https://g.example/"
    monkeypatch.setattr(service.fs, "load_global", lambda: {"seed_url": "off"})
    assert service.seed_url() is None
