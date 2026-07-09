"""Tests for the seed step wired into the gather sequencer (`_maybe_seed` and its
decision helpers). The decision logic is exercised as pure functions; the
end-to-end seed is driven against a real file-backed store (no network, no full
gather) by calling `_maybe_seed` directly with a parsed args namespace.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from ietf_llm import freshness
from ietf_llm.log import Verbosity
from ietf_llm.gather.cli import build_parser
from ietf_llm.gather.sequencer import (
    _maybe_seed,
    _seed_covers_config,
    _seed_decision,
)
from ietf_llm.paths import get_cache_dir, get_index_dir
from ietf_llm.seed import format as fmt
from ietf_llm.seed import publish


def _entry(gathered, window=12):
    return fmt.IndexEntry("httpbis", "group", "HTTP WG", window, gathered,
                          "v", "httpbis/manifest.json", 1)


def _args(**over):
    base = dict(months=12, refresh_base=False, new_drafts=False, author=None,
                add_mentioned_drafts=False, draft=[], mailing_list=[])
    base.update(over)
    return SimpleNamespace(**base)


# --- pure decision logic ---------------------------------------------------- #


def test_decision_cold_when_no_prior():
    assert _seed_decision(_args(), _entry("2026-07-01T00:00:00+00:00"), None) == "cold"


def test_decision_refresh_flag():
    prior = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert _seed_decision(_args(refresh_base=True),
                          _entry("2026-01-01T00:00:00+00:00"), prior) == "refresh"


def test_decision_stale_when_snapshot_newer():
    prior = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _seed_decision(_args(), _entry("2026-07-01T00:00:00+00:00"), prior) == "stale"


def test_decision_none_when_local_fresher():
    prior = datetime(2026, 7, 5, tzinfo=timezone.utc)
    assert _seed_decision(_args(), _entry("2026-07-01T00:00:00+00:00"), prior) is None


def test_covers_config_window_must_not_narrow():
    assert not _seed_covers_config(_args(months=24), _entry("g", window=12))
    assert _seed_covers_config(_args(months=12), _entry("g", window=12))
    assert not _seed_covers_config(_args(months=0), _entry("g", window=12))


def test_covers_config_custom_sources_block_stalejump():
    assert not _seed_covers_config(_args(draft=["draft-x"]), _entry("g"))
    assert not _seed_covers_config(_args(mailing_list=["extra"]), _entry("g"))
    assert not _seed_covers_config(_args(new_drafts=True), _entry("g"))
    assert not _seed_covers_config(_args(author="Jane"), _entry("g"))


def test_stalejump_refused_when_window_narrows():
    prior = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Snapshot newer but only a 6-month window vs the configured 12.
    assert _seed_decision(_args(months=12),
                          _entry("2026-07-01T00:00:00+00:00", window=6), prior) is None


# --- end-to-end _maybe_seed against a real file store ----------------------- #


def _gathered(name, *, model="sentence-transformers/BAAI/bge-small-en-v1.5"):
    files = os.path.join(get_cache_dir(), name, "files")
    os.makedirs(files, exist_ok=True)
    with open(os.path.join(files, "charter.txt"), "w") as fh:
        fh.write(f"charter {name}")
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


def _store(tmp_path, name="httpbis", **kw):
    store = str(tmp_path / "store")
    _gathered(name, **kw)
    publish.publish_store(store, add=[name], no_gather=True, gather=lambda n, m: None)
    return store


def _seed_args(wg, **over):
    args = build_parser().parse_args([wg])
    for key, val in over.items():
        setattr(args, key, val)
    return args


def test_maybe_seed_cold_installs(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path)
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))  # cold consumer
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    _maybe_seed(_seed_args("httpbis"), on_cloud=False, verbosity=Verbosity.QUIET)
    assert os.path.isfile(
        os.path.join(get_cache_dir(), "httpbis", "files", "charter.txt"))


def test_maybe_seed_not_covered_noop(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path, name="tls")
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    _maybe_seed(_seed_args("httpbis"), on_cloud=False, verbosity=Verbosity.QUIET)
    assert not os.path.exists(os.path.join(get_cache_dir(), "httpbis"))


def test_maybe_seed_no_seed_flag(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path)
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    _maybe_seed(_seed_args("httpbis", no_seed=True), on_cloud=False, verbosity=Verbosity.QUIET)
    assert not os.path.exists(os.path.join(get_cache_dir(), "httpbis"))


def test_maybe_seed_disabled_when_no_url(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path)
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    monkeypatch.delenv("IETF_LLM_SEED_URL", raising=False)
    _maybe_seed(_seed_args("httpbis"), on_cloud=False, verbosity=Verbosity.QUIET)
    assert not os.path.exists(os.path.join(get_cache_dir(), "httpbis"))


def test_maybe_seed_cloud_backend_noop(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path)
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    _maybe_seed(_seed_args("httpbis"), on_cloud=True, verbosity=Verbosity.QUIET)
    assert not os.path.exists(os.path.join(get_cache_dir(), "httpbis"))


def test_maybe_seed_model_mismatch_noop(isolated_home, tmp_path, monkeypatch):
    store = _store(tmp_path, model="openai-embed/text-embedding-3-small")
    shutil.rmtree(os.path.join(get_cache_dir(), "httpbis"))
    monkeypatch.setenv("IETF_LLM_SEED_URL", store)
    _maybe_seed(_seed_args("httpbis"), on_cloud=False, verbosity=Verbosity.QUIET)
    # Default client model != the store's model → no install.
    assert not os.path.exists(os.path.join(get_cache_dir(), "httpbis"))
