"""Tests for the seed-store publisher (`ietf_llm.seed.publish`).

Uses the isolated_home sandbox so the local cache is a tmp dir. Gathering is
stubbed (a no-op or a recorder) — these exercise membership persistence,
bundling, up-to-date skipping, force, prune, the one-model-per-store guardrail,
and dry-run, not a real gather. Covers:
- --add / --remove persist members.json; a bare run publishes members
- a published corpus yields index.json + manifest + bundle, and re-publishing is
  skipped as up-to-date (--force overrides)
- a second corpus on a different embedding model is refused
- --prune drops non-members; --dry-run writes nothing
- an unknown positional corpus raises PublishError
- the default gather step is invoked with (corpus, window)
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from ietf_llm import freshness
from ietf_llm.embeddings.storage import _SCHEMA_VERSION
from ietf_llm.paths import get_cache_dir, get_index_dir
from ietf_llm.seed import format as fmt
from ietf_llm.seed import publish


def _gathered(name, *, model="sentence-transformers/BAAI/bge-small-en-v1.5"):
    """Materialise a minimal gathered corpus in the isolated cache."""
    corpus_dir = os.path.join(get_cache_dir(), name)
    files = os.path.join(corpus_dir, "files")
    os.makedirs(files, exist_ok=True)
    with open(os.path.join(files, "charter.txt"), "w") as fh:
        fh.write(f"charter for {name}")
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


def _no_gather(name, months):
    pass


def test_add_persists_members(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    publish.publish_store(store, add=["httpbis"], months=6, gather=_no_gather,
                          no_gather=True)
    members = publish.load_members(store)
    assert members["httpbis"].window_months == 6


def test_publish_creates_index_manifest_bundle(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("httpbis")
    publish.publish_store(store, add=["httpbis"], no_gather=True, gather=_no_gather)
    assert os.path.isfile(os.path.join(store, "index.json"))
    assert os.path.isfile(os.path.join(store, "httpbis", "manifest.json"))
    idx = fmt.Index.from_json(open(os.path.join(store, "index.json")).read())
    entry = idx.entry("httpbis")
    assert entry is not None
    assert os.path.isfile(os.path.join(store, entry.name, os.path.basename(
        fmt.manifest_from_json(open(os.path.join(store, entry.manifest)).read()).bundle)))
    assert idx.compat.embedding_model.endswith("bge-small-en-v1.5")


def test_uptodate_skip_then_force(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("httpbis")
    publish.publish_store(store, add=["httpbis"], no_gather=True, gather=_no_gather)
    # Second run without a re-gather: same last-gathered -> same version -> skip.
    r2 = publish.publish_store(store, no_gather=True, gather=_no_gather)
    assert r2.uptodate == ["httpbis"]
    assert not r2.published
    # --force re-bundles it.
    r3 = publish.publish_store(store, force=True, no_gather=True, gather=_no_gather)
    assert [n for n, _, _ in r3.published] == ["httpbis"]


def test_model_mismatch_refused(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("aaa")  # default model — sorts first, sets the store tuple
    _gathered("zzz", model="openai-embed/text-embedding-3-small")
    report = publish.publish_store(
        store, add=["aaa", "zzz"], no_gather=True, gather=_no_gather)
    assert [n for n, _, _ in report.published] == ["aaa"]
    assert any(n == "zzz" for n, _ in report.skipped)
    idx = fmt.Index.from_json(open(os.path.join(store, "index.json")).read())
    assert idx.entry("zzz") is None


def test_prune_drops_non_members(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("httpbis")
    _gathered("tls")
    publish.publish_store(store, add=["httpbis", "tls"], no_gather=True,
                          gather=_no_gather)
    report = publish.publish_store(
        store, remove=["tls"], prune=True, no_gather=True, gather=_no_gather)
    assert report.pruned == ["tls"]
    assert not os.path.exists(os.path.join(store, "tls"))
    idx = fmt.Index.from_json(open(os.path.join(store, "index.json")).read())
    assert idx.entry("tls") is None and idx.entry("httpbis") is not None


def test_dry_run_writes_nothing(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("httpbis")
    report = publish.publish_store(
        store, add=["httpbis"], dry_run=True, no_gather=True, gather=_no_gather)
    assert not os.path.exists(os.path.join(store, "index.json"))
    assert not os.path.exists(os.path.join(store, publish.MEMBERS_NAME))
    assert [n for n, _, _ in report.published] == ["httpbis"]


def test_unknown_positional_raises(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    with pytest.raises(publish.PublishError):
        publish.publish_store(store, process=["nope"], gather=_no_gather)


def test_default_gather_invoked(isolated_home, tmp_path):
    store = str(tmp_path / "store")
    _gathered("httpbis")
    calls = []
    publish.publish_store(
        store, add=["httpbis"], months=9,
        gather=lambda name, months: calls.append((name, months)))
    assert calls == [("httpbis", 9)]


def test_gather_failure_skips(isolated_home, tmp_path):
    store = str(tmp_path / "store")

    def _boom(name, months):
        raise publish.PublishError("gather blew up")

    report = publish.publish_store(store, add=["httpbis"], gather=_boom)
    assert any(n == "httpbis" for n, _ in report.skipped)
    assert not report.published
