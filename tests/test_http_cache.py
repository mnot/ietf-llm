"""Tests for the datatracker conditional-GET (ETag) store: the on-disk layout
(an index plus one gzip file per body), eviction, and migration of the older
single-file blob. The 304 network path itself is exercised live, not here;
`_get_json` is driven against a stubbed `governed_get`.
"""

from __future__ import annotations

import atexit
import gzip
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from ietf_llm.gather.sources import datatracker, http_cache
from ietf_llm.gather.sources.datatracker import _decode_cached

# The real function, captured before conftest's autouse fixture stubs it.
real_get_json = datatracker._get_json
from ietf_llm.gather.sources.http_cache import INDEX_NAME, MAX_AGE_DAYS, HttpCache

DAY = 86400.0


def _dest() -> str:
    return datatracker.http_cache_path()


def _legacy() -> str:
    return datatracker.legacy_http_cache_path()


def _new_cache(**kwargs: Any) -> HttpCache:
    """A cache bound to the default on-disk path under the (isolated) cache dir.
    Tests flush by hand, so none registers an atexit flush unless it asks to."""
    kwargs.setdefault("persist_at_exit", False)
    return HttpCache(_dest(), legacy=_legacy(), **kwargs)


def _bodies(directory: str) -> list[str]:
    return sorted(n for n in os.listdir(directory) if http_cache.BODY_NAME.match(n))


def _index(directory: str) -> Dict[str, Any]:
    with open(os.path.join(directory, INDEX_NAME), encoding="utf-8") as fh:
        return json.load(fh)["entries"]


def test_store_load_roundtrip(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("https://x/api?format=json", 'W/"abc"', '{"k": 1}')
    cache.flush()
    # A fresh instance reads what the first one wrote.
    reloaded = _new_cache()
    entry = reloaded.get("https://x/api?format=json")
    assert entry is not None
    assert entry["etag"] == 'W/"abc"'
    assert reloaded.body("https://x/api?format=json") == '{"k": 1}'
    assert "last_used" in entry  # tracked for eviction
    # It landed at the machinery path, not inside any WG corpus, as one gzip file
    # per body plus the index.
    assert os.path.isfile(os.path.join(_dest(), INDEX_NAME))
    [name] = _bodies(_dest())
    with open(os.path.join(_dest(), name), "rb") as fh:
        assert gzip.decompress(fh.read()) == b'{"k": 1}'


def test_a_gather_loads_only_the_bodies_it_revalidates(isolated_home: Path) -> None:
    cache = _new_cache()
    for i in range(3):
        cache.store(f"u{i}", "e", f"body {i}")
    cache.flush()
    reloaded = _new_cache()
    assert reloaded.get("u1") is not None
    assert all("body" not in entry for entry in reloaded._entries.values())  # type: ignore[union-attr]
    assert reloaded.body("u1") == "body 1"


def test_flush_targets_construction_dir_not_exit_time(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Regression: the deferred flush must write to the cache dir captured when
    the cache was constructed, not re-resolve get_cache_dir() at flush time.

    The corruption this guards against: `make test` exercises a store under
    isolated_home, then the monkeypatch is reverted, and only later does the
    atexit flush fire -- re-resolving get_cache_dir() to the developer's real
    ~/.cache/ietf-llm/ and overwriting their live store with the test's junk.
    """
    bound = _dest()
    cache = HttpCache(bound, legacy=_legacy())  # dest captured now
    cache.store("https://x/api?format=json", 'W/"abc"', '{"k": 1}')

    # Repoint HOME the way monkeypatch teardown + a deferred exit-time
    # flush would: the cache dir now resolves somewhere new.
    other = tmp_path_factory.mktemp("elsewhere")
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("USERPROFILE", str(other))
    assert not _dest().startswith(os.path.dirname(os.path.dirname(bound)))

    cache.flush()
    atexit.unregister(cache.flush)

    # The entries landed where the cache was constructed, not under the new HOME.
    assert os.path.isfile(os.path.join(bound, INDEX_NAME))
    assert not os.path.exists(_dest())


def test_missing_url_is_none(isolated_home: Path) -> None:
    cache = _new_cache()
    assert cache.get("https://nope/") is None
    assert cache.body("https://nope/") is None


@pytest.mark.parametrize(
    "junk", ["{broken", "[]", '{"version": 99, "entries": {}}', '{"version": 1}']
)
def test_tolerates_a_corrupt_index(isolated_home: Path, junk: str) -> None:
    os.makedirs(_dest())
    with open(os.path.join(_dest(), INDEX_NAME), "w", encoding="utf-8") as fh:
        fh.write(junk)
    cache = _new_cache()
    assert cache.get("anything") is None  # no crash -> empty
    cache.store("u", "e", "{}")  # and usable afterwards
    cache.flush()
    assert _new_cache().get("u") is not None


def test_an_index_entry_cannot_name_a_file_outside_the_store(
    isolated_home: Path,
) -> None:
    """The index may come out of a restored archive; its file names are joined to
    a directory path."""
    os.makedirs(_dest())
    entries = {
        "u": {"etag": "e", "file": "../../escape.gz", "size": 1, "last_used": 1.0},
        "ok": {"etag": "e", "file": "a" * 64 + ".gz", "size": 1, "last_used": 1.0},
    }
    with open(os.path.join(_dest(), INDEX_NAME), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": entries}, fh)
    index = http_cache.read_index(_dest())
    assert index is not None and set(index) == {"ok"}


def test_an_entry_whose_body_file_is_gone_is_dropped(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("u", "e", "{}")
    cache.flush()
    os.remove(os.path.join(_dest(), _bodies(_dest())[0]))
    reloaded = _new_cache()
    assert reloaded.get("u") is None  # no ETag offered for a body we cannot honour
    reloaded.flush()
    assert "u" not in _index(_dest())


def test_a_corrupt_body_file_is_dropped(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("u", "e", "{}")
    cache.flush()
    name = _bodies(_dest())[0]
    with open(os.path.join(_dest(), name), "wb") as fh:
        fh.write(b"not gzip")
    reloaded = _new_cache()
    assert reloaded.get("u") is not None
    assert reloaded.body("u") is None
    assert reloaded.get("u") is None
    reloaded.flush()
    assert not os.path.exists(os.path.join(_dest(), name))


def test_restoring_a_url_replaces_its_body_at_flush(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("u", "e1", '{"v": 1}')
    cache.flush()
    cache.store("u", "e2", '{"v": 2}')
    # The index on disk still describes the first body, which must survive until
    # the new index is written.
    assert len(_bodies(_dest())) == 2
    cache.flush()
    assert len(_bodies(_dest())) == 1
    reloaded = _new_cache()
    entry = reloaded.get("u")
    assert entry is not None and entry["etag"] == "e2"
    assert reloaded.body("u") == '{"v": 2}'


def test_evict_drops_stale_keeps_fresh(isolated_home: Path) -> None:
    """The age sweep drops entries unused past the window but keeps a fresh
    one -- so an infrequently-gathered endpoint isn't re-downloaded."""
    cache = _new_cache()
    cache.store("fresh", "a", "{}")
    cache.store("stale", "b", "{}")
    cache._entries["stale"]["last_used"] = (  # type: ignore[index]
        time.time() - (MAX_AGE_DAYS + 1) * DAY
    )
    cache.flush()
    assert len(_bodies(_dest())) == 1  # the stale body went with its entry
    reloaded = _new_cache()
    assert reloaded.get("stale") is None
    assert reloaded.get("fresh") is not None


def test_evict_caps_entry_count_keeping_newest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_cache, "MAX_ENTRIES", 2)
    now = time.time()
    cache = _new_cache()
    for i in range(4):
        cache.store(f"u{i}", "x", "{}")
        cache._entries[f"u{i}"]["last_used"] = now - i  # type: ignore[index]
    cache.flush()  # u0 newest ... u3 oldest
    assert set(_index(_dest())) == {"u0", "u1"}
    assert len(_bodies(_dest())) == 2


def test_evict_caps_total_bytes_keeping_newest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    cache = _new_cache()
    for i in range(3):
        cache.store(f"u{i}", "x", f"{i}" * 4000)
        cache._entries[f"u{i}"]["last_used"] = now - i  # type: ignore[index]
    one = cache._entries["u0"]["size"]  # type: ignore[index]
    monkeypatch.setattr(http_cache, "MAX_BYTES", one * 2 + 1)
    cache.flush()
    assert set(_index(_dest())) == {"u0", "u1"}
    assert len(_bodies(_dest())) == 2


def test_stray_files_are_swept_once_old_enough(isolated_home: Path) -> None:
    """Another process sharing the directory may have written a body its index
    doesn't name yet, so a stray is only deleted after a day."""
    cache = _new_cache()
    cache.store("u", "e", "{}")
    cache.flush()
    old = os.path.join(_dest(), "b" * 64 + ".gz")
    young = os.path.join(_dest(), "c" * 64 + ".gz")
    for path in (old, young):
        with open(path, "wb") as fh:
            fh.write(b"x")
    long_ago = time.time() - 2 * DAY
    os.utime(old, (long_ago, long_ago))
    cache.store("u2", "e", "{}")
    cache.flush()
    assert not os.path.exists(old)
    assert os.path.exists(young)


# --- migrating the single-file blob ------------------------------------------


def _write_legacy(entries: Any) -> None:
    os.makedirs(os.path.dirname(_legacy()), exist_ok=True)
    with open(_legacy(), "w", encoding="utf-8") as fh:
        json.dump(entries, fh)


def test_a_single_file_blob_is_migrated(isolated_home: Path) -> None:
    used = time.time() - 3 * DAY
    _write_legacy(
        {
            "u1": {"etag": "e1", "body": '{"a": 1}', "last_used": used},
            "u2": {"etag": "e2", "body": '{"b": 2}'},  # predates last_used
        }
    )
    cache = _new_cache()
    entry = cache.get("u1")
    assert entry is not None and entry["etag"] == "e1"
    assert cache.body("u1") == '{"a": 1}'
    assert cache.body("u2") == '{"b": 2}'
    cache.flush()
    assert not os.path.exists(_legacy())  # superseded once the index is on disk
    assert len(_bodies(_dest())) == 2
    reloaded = _new_cache()
    assert reloaded.body("u2") == '{"b": 2}'
    assert reloaded.get("u2") is not None


def test_the_blob_is_kept_until_the_index_is_written(isolated_home: Path) -> None:
    _write_legacy({"u": {"etag": "e", "body": "{}"}})
    cache = _new_cache()
    cache.get("u")  # loads and migrates, but nothing is flushed
    assert os.path.exists(_legacy())


@pytest.mark.parametrize(
    "blob",
    [
        '{"u": {"etag": "e", "body": "{}"',  # truncated mid-write
        "garbage",
        "",
        "[]",
        "[" * 100000,  # deep enough to overflow the parser's recursion limit
        '{"u": 5, "v": {"etag": 1, "body": "{}"}, "w": {"etag": "e"}}',
        '{"u": {"etag": "e", "body": "\\ud800"}}',  # a lone surrogate
    ],
)
def test_a_corrupt_blob_never_crashes_and_yields_nothing(
    isolated_home: Path, blob: str
) -> None:
    os.makedirs(os.path.dirname(_legacy()), exist_ok=True)
    with open(_legacy(), "w", encoding="utf-8") as fh:
        fh.write(blob)
    cache = _new_cache()
    assert cache.get("u") is None
    cache.store("fresh", "e", "{}")  # usable, and flushes cleanly
    cache.flush()
    assert set(_index(_dest())) == {"fresh"}


def test_a_blob_with_some_sound_entries_keeps_those(isolated_home: Path) -> None:
    _write_legacy(
        {
            "bad": {"etag": 1, "body": "{}"},
            "good": {"etag": "e", "body": '{"ok": true}', "last_used": "soon"},
        }
    )
    cache = _new_cache()
    assert cache.get("bad") is None
    assert cache.body("good") == '{"ok": true}'  # an unusable timestamp is "now"


def test_an_existing_index_wins_over_a_leftover_blob(isolated_home: Path) -> None:
    cache = _new_cache()
    cache.store("new", "e", "{}")
    cache.flush()
    _write_legacy({"old": {"etag": "e", "body": "{}"}})
    again = _new_cache()
    assert again.get("old") is None
    assert again.get("new") is not None
    again.flush()
    assert not os.path.exists(_legacy())


# --- _get_json over the store ------------------------------------------------


class _Resp:
    def __init__(self, status: int, text: str = "", etag: Optional[str] = None) -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"ETag": etag} if etag else {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return json.loads(self.text)


class _Net:
    """The stubbed network: queue answers in `replies`, read requests off `seen`."""

    def __init__(self) -> None:
        self.replies: list[_Resp] = []
        self.seen: list[Dict[str, str]] = []

    def get(self, url: str, headers: Dict[str, str], timeout: float) -> _Resp:
        self.seen.append(headers)
        return self.replies.pop(0)


@pytest.fixture
def net(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> _Net:
    datatracker.bind_gather_corpus(None)
    monkeypatch.setattr(datatracker, "_DEFAULT_CACHE", None)
    stub = _Net()
    monkeypatch.setattr(datatracker, "governed_get", stub.get)
    return stub


def _fresh_process() -> None:
    """Flush the default store and forget it, as a gather's exit and the next
    start do. The atexit hook is dropped so it cannot fire after teardown."""
    store = datatracker._default_http_cache()
    store.flush()
    atexit.unregister(store.flush)
    datatracker._DEFAULT_CACHE = None


_URL = "https://datatracker.ietf.org/api/v1/x/"


def test_get_json_revalidates_and_reuses_the_stored_body(net: _Net) -> None:
    net.replies += [_Resp(200, '{"objects": [1]}', 'W/"v1"'), _Resp(304)]
    assert real_get_json(_URL) == {"objects": [1]}
    _fresh_process()
    # The ETag comes off disk, and so does the body the 304 reuses.
    assert real_get_json(_URL) == {"objects": [1]}
    assert net.seen[1]["If-None-Match"] == 'W/"v1"'
    _fresh_process()


def test_get_json_survives_a_304_whose_body_is_unreadable(net: _Net) -> None:
    net.replies += [_Resp(200, '{"objects": [1]}', 'W/"v1"'), _Resp(304)]
    real_get_json(_URL)
    _fresh_process()
    for name in _bodies(_dest()):
        with open(os.path.join(_dest(), name), "wb") as fh:
            fh.write(b"not gzip")
    assert real_get_json(_URL) is None  # a miss, not a crash
    _fresh_process()


def test_decode_cached_parses_body() -> None:
    assert _decode_cached('{"a": 2}') == {"a": 2}


def test_decode_cached_handles_bad_input() -> None:
    assert _decode_cached(None) is None
    assert _decode_cached("not json") is None
    assert _decode_cached(json.dumps([1, 2])) is None  # not a dict
