"""The per-host gather egress governor (`http_governor`).

These verify the load-bearing property: a host's slot pool bounds how many
requests are in flight to it at once, independently of how many threads pile
in, and distinct hosts don't share a pool. The cap is read from
`service_config`, so the env knobs steer it.
"""

from __future__ import annotations

import threading
import time

import pytest

from ietf_llm.net import http_governor
from ietf_llm.net.http_governor import host_slot


@pytest.fixture(autouse=True)
def _reset_governor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean per-host semaphore table and no env caps
    bleeding in from the shell."""
    monkeypatch.delenv("IETF_LLM_HTTP_MAX_PER_HOST", raising=False)
    monkeypatch.delenv("IETF_LLM_HTTP_MAX_DATATRACKER", raising=False)
    http_governor.reset()
    yield
    http_governor.reset()


def test_host_slot_caps_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IETF_LLM_HTTP_MAX_DATATRACKER", "2")
    http_governor.reset()
    url = "https://datatracker.ietf.org/api/v1/x"

    lock = threading.Lock()
    active = 0
    peak = 0
    entered = threading.Semaphore(0)
    release = threading.Event()

    def worker() -> None:
        nonlocal active, peak
        with host_slot(url):
            with lock:
                active += 1
                peak = max(peak, active)
            entered.release()
            release.wait(2.0)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    # Two should get in immediately; give any (wrongly admitted) third a moment.
    assert entered.acquire(timeout=2.0)
    assert entered.acquire(timeout=2.0)
    time.sleep(0.05)
    with lock:
        assert peak == 2
    release.set()
    for thread in threads:
        thread.join(2.0)
    assert peak == 2


def test_distinct_hosts_have_independent_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both pools sized to 1: holding one host's only slot must not stop a
    # different host's slot from being taken.
    monkeypatch.setenv("IETF_LLM_HTTP_MAX_PER_HOST", "1")
    monkeypatch.setenv("IETF_LLM_HTTP_MAX_DATATRACKER", "1")
    http_governor.reset()

    got = threading.Event()

    def grab_other() -> None:
        with host_slot("https://www.ietf.org/archive/id/draft-00.txt"):
            got.set()

    with host_slot("https://datatracker.ietf.org/api/v1/x"):
        thread = threading.Thread(target=grab_other)
        thread.start()
        thread.join(2.0)
        assert got.is_set()


def test_cap_change_takes_effect_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_HTTP_MAX_PER_HOST", "4")
    http_governor.reset()
    # Pool is built lazily from config; _initial_value is the cap it was sized
    # with (a BoundedSemaphore implementation detail, used here as a probe).
    sem = http_governor._sem_for("example.com")
    assert sem._initial_value == 4  # pylint: disable=protected-access


def test_default_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    general = http_governor._sem_for("example.com")
    datatracker = http_governor._sem_for("datatracker.ietf.org")
    assert general._initial_value == 6  # pylint: disable=protected-access
    assert datatracker._initial_value == 2  # pylint: disable=protected-access


def test_invalid_cap_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("0", "-3", "nope", ""):
        monkeypatch.setenv("IETF_LLM_HTTP_MAX_PER_HOST", bad)
        http_governor.reset()
        sem = http_governor._sem_for("example.com")
        assert sem._initial_value == 6  # pylint: disable=protected-access
