"""Tests for the datatracker conditional-GET (ETag) cache internals:
the on-disk store round-trip and cached-body decoding. The 304 network
path itself is exercised live, not here.
"""

from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path

import pytest

from ietf_llm.gather.sources import datatracker
from ietf_llm.gather.sources.datatracker import _CACHE_MAX_AGE_DAYS, _decode_cached, _HttpCache
from ietf_llm.paths import get_cache_dir


def _new_cache() -> _HttpCache:
    """A cache bound to the default on-disk path under the (isolated) cache dir."""
    return _HttpCache(os.path.join(get_cache_dir(), ".http-cache.json"))


def test_http_cache_store_load_roundtrip(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("https://x/api?format=json", 'W/"abc"', '{"k": 1}')
    cache.flush()
    # store() schedules a deferred flush at interpreter exit; drop it so
    # this test instance can't write again after isolated_home tears down.
    atexit.unregister(cache.flush)
    # A fresh instance reads what the first one wrote.
    reloaded = _new_cache()
    entry = reloaded.get("https://x/api?format=json")
    atexit.unregister(reloaded.flush)  # get() touches last_used → schedules a flush
    assert entry is not None
    assert entry["etag"] == 'W/"abc"'
    assert entry["body"] == '{"k": 1}'
    assert "last_used" in entry  # tracked for eviction
    # And it landed at the machinery path, not inside any WG corpus.
    assert os.path.isfile(os.path.join(get_cache_dir(), ".http-cache.json"))


def test_flush_targets_construction_dir_not_exit_time(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Regression: the deferred flush must write to the cache dir captured when
    the cache was constructed, not re-resolve get_cache_dir() at flush time.

    The corruption this guards against: `make test` exercises an `_HttpCache`
    under isolated_home (HOME -> tmp), then the monkeypatch is reverted, and only
    later does the atexit flush fire — re-resolving get_cache_dir() to the
    developer's real ~/.cache/ietf-llm/ and overwriting their live
    .http-cache.json with the test's junk entries.
    """
    cache = _new_cache()  # dest captured now, under isolated_home
    cache.store("https://x/api?format=json", 'W/"abc"', '{"k": 1}')
    bound = os.path.join(get_cache_dir(), ".http-cache.json")

    # Repoint HOME the way monkeypatch teardown + a deferred exit-time
    # flush would: the cache dir now resolves somewhere new.
    other = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("USERPROFILE", str(other))
    assert get_cache_dir() != os.path.dirname(bound)  # HOME really moved

    cache.flush()
    atexit.unregister(cache.flush)

    # The entries landed where the cache was constructed, not under the new HOME.
    assert os.path.isfile(bound)
    stray = os.path.join(get_cache_dir(), ".http-cache.json")
    assert not os.path.exists(stray)


def test_http_cache_missing_url_is_none(isolated_home: Path) -> None:
    assert _new_cache().get("https://nope/") is None


def test_http_cache_tolerates_corrupt_file(isolated_home: Path) -> None:
    path = os.path.join(get_cache_dir(), ".http-cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert _new_cache().get("anything") is None  # no crash → empty


def test_evict_drops_stale_keeps_fresh() -> None:
    """The age sweep drops entries unused past the window but keeps a fresh
    one — so an infrequently-gathered endpoint isn't re-downloaded."""
    now = time.time()
    day = 86400.0
    cache = _HttpCache("unused")  # _evict works on _entries; never touches disk
    cache._entries = {
        "fresh": {"etag": "a", "body": "{}", "last_used": now - 5 * day},
        "stale": {
            "etag": "b",
            "body": "{}",
            "last_used": now - (_CACHE_MAX_AGE_DAYS + 1) * day,
        },
    }
    cache._evict(now)
    assert set(cache._entries) == {"fresh"}


def test_evict_legacy_entry_without_timestamp_survives() -> None:
    """An entry predating last_used tracking is treated as seen `now`, so a
    legacy cache isn't wiped on the first post-change flush."""
    now = time.time()
    cache = _HttpCache("unused")
    cache._entries = {"legacy": {"etag": "a", "body": "{}"}}  # no last_used
    cache._evict(now)
    assert set(cache._entries) == {"legacy"}


def test_evict_caps_entry_count_keeping_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the entry cap, only the most-recently-used survive."""
    monkeypatch.setattr(datatracker, "_CACHE_MAX_ENTRIES", 2)
    now = time.time()
    cache = _HttpCache("unused")
    cache._entries = {
        f"u{i}": {"etag": "x", "body": "{}", "last_used": now - i}
        for i in range(4)
    }  # u0 newest … u3 oldest
    cache._evict(now)
    assert set(cache._entries) == {"u0", "u1"}


def test_flush_then_reload_drops_stale_entry(isolated_home: Path) -> None:
    """Round-trip: a stale entry is gone after flush, a fresh one persists."""
    cache = _new_cache()
    cache.store("https://fresh/api?format=json", "a", "{}")
    cache.store("https://stale/api?format=json", "b", "{}")
    # Backdate the stale entry past the age window.
    cache._entries["https://stale/api?format=json"]["last_used"] = (
        time.time() - (_CACHE_MAX_AGE_DAYS + 1) * 86400.0
    )
    cache.flush()
    atexit.unregister(cache.flush)

    reloaded = _new_cache()
    assert reloaded.get("https://stale/api?format=json") is None
    assert reloaded.get("https://fresh/api?format=json") is not None
    atexit.unregister(reloaded.flush)  # get() touched last_used → scheduled a flush


def test_decode_cached_parses_body() -> None:
    assert _decode_cached({"etag": "x", "body": '{"a": 2}'}) == {"a": 2}


def test_decode_cached_handles_bad_input() -> None:
    assert _decode_cached(None) is None
    assert _decode_cached({"body": "not json"}) is None
    assert _decode_cached({"body": json.dumps([1, 2])}) is None  # not a dict
