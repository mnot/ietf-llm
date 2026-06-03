"""Tests for the datatracker conditional-GET (ETag) cache internals:
the on-disk store round-trip and cached-body decoding. The 304 network
path itself is exercised live, not here.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path

import pytest

from ietf_llm.gather.datatracker import _decode_cached, _HttpCache
from ietf_llm.utils import get_cache_dir


def test_http_cache_store_load_roundtrip(isolated_home: Path) -> None:
    cache = _HttpCache()
    cache.store("https://x/api?format=json", 'W/"abc"', '{"k": 1}')
    cache.flush()
    # store() schedules a deferred flush at interpreter exit; drop it so
    # this test instance can't write again after isolated_home tears down.
    atexit.unregister(cache.flush)
    # A fresh instance reads what the first one wrote.
    reloaded = _HttpCache()
    entry = reloaded.get("https://x/api?format=json")
    assert entry == {"etag": 'W/"abc"', "body": '{"k": 1}'}
    # And it landed at the machinery path, not inside any WG corpus.
    assert os.path.isfile(os.path.join(get_cache_dir(), ".http-cache.json"))


def test_flush_targets_load_time_cache_dir_not_exit_time(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Regression: the deferred flush must write to the cache dir resolved
    when the cache was first used, not re-resolve get_cache_dir() at flush
    time.

    The corruption this guards against: `make test` exercises an
    `_HttpCache` under isolated_home (HOME -> tmp), then the monkeypatch is
    reverted, and only later does the atexit flush fire — re-resolving
    get_cache_dir() to the developer's real ~/.cache/ietf-llm/ and
    overwriting their live .http-cache.json with the test's junk entries.
    """
    cache = _HttpCache()
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

    # The entries landed where the cache first loaded, not under the new HOME.
    assert os.path.isfile(bound)
    stray = os.path.join(get_cache_dir(), ".http-cache.json")
    assert not os.path.exists(stray)


def test_http_cache_missing_url_is_none(isolated_home: Path) -> None:
    assert _HttpCache().get("https://nope/") is None


def test_http_cache_tolerates_corrupt_file(isolated_home: Path) -> None:
    path = os.path.join(get_cache_dir(), ".http-cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert _HttpCache().get("anything") is None  # no crash → empty


def test_decode_cached_parses_body() -> None:
    assert _decode_cached({"etag": "x", "body": '{"a": 2}'}) == {"a": 2}


def test_decode_cached_handles_bad_input() -> None:
    assert _decode_cached(None) is None
    assert _decode_cached({"body": "not json"}) is None
    assert _decode_cached({"body": json.dumps([1, 2])}) is None  # not a dict
