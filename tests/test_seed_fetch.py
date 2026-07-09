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

import pytest

from ietf_llm import freshness
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
    db = os.path.join(get_index_dir(), name, "embeddings.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("model", model), ("schema_version", "8"),
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


def test_seed_url_disable_semantics(isolated_home, monkeypatch):
    monkeypatch.delenv("IETF_LLM_SEED_URL", raising=False)
    assert service.seed_url() is None  # default None until hosting chosen
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
