"""A caller's regex is bounded: killed at the budget, off the shared worker pool.

`re` cannot be interrupted, so a catastrophic pattern would otherwise run
forever on an abandoned `_offload` worker and hold one of anyio's 40 default
tokens. These tests drive a genuinely pathological pattern and assert the three
things that matter: the reply says the scan is incomplete (never an absence),
it returns near the budget, and nothing is left running or borrowed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any, List

import anyio
import numpy as np
import pytest

from ietf_llm import regexscan
from ietf_llm.embeddings.storage import _open_db
from ietf_llm.mcp import common, grep, grep_rfc, rfc_text
from ietf_llm.mcp.grep import tool_grep_corpus

from conftest import write_cache_file

#: Exponential on a run of word characters followed by a failing tail; 40 of
#: them is ~2**40 steps, i.e. effectively forever.
_EVIL = r"(\w|\w)*X"
_EVIL_TEXT = "a" * 40 + "!\n"


@pytest.fixture(name="tight")
def _tight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the budgets so the kill path takes a second or two, not 20+."""
    monkeypatch.setattr(grep, "_REGEX_SCAN_BUDGET_S", 0.5)
    monkeypatch.setattr(grep_rfc, "_RFC_SCAN_BUDGET_S", 0.5)
    monkeypatch.setattr(regexscan, "KILL_SLACK_S", 0.5)


@pytest.fixture(name="spawned")
def _spawned(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """The pids of the scan children started during the test."""
    pids: List[int] = []
    real = subprocess.Popen

    class _Recording(real):  # type: ignore[type-arg, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            pids.append(self.pid)

    monkeypatch.setattr(regexscan.subprocess, "Popen", _Recording)
    return pids


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_pathological_regex_is_killed_and_not_called_absence(
    isolated_home: Path, tight: None, spawned: List[int]
) -> None:
    assert tight is None
    write_cache_file(isolated_home, "wg", "threads/evil.md", _EVIL_TEXT)
    started = time.monotonic()
    out = tool_grep_corpus("wg", _EVIL, regex=True)
    assert time.monotonic() - started < 6.0
    assert "INCOMPLETE SCAN" in out
    assert "cannot support a negative claim" in out
    assert "no matches for" not in out
    assert "evidence of absence" not in out
    assert len(spawned) == 1 and _gone(spawned[0])


def test_pathological_regex_over_the_rfc_series_is_killed(
    isolated_home: Path, tight: None, spawned: List[int]
) -> None:
    assert tight is None and isolated_home
    conn = _open_db(rfc_text.RFC_CORPUS)
    try:
        conn.execute(
            "INSERT INTO chunks(file, chunk_idx, sub_idx, title, text, embedding, "
            "section) VALUES('rfc1.txt', 0, 0, 'Intro', ?, ?, '1')",
            (_EVIL_TEXT, np.ones(4, dtype=np.float32).tobytes()),
        )
        conn.commit()
    finally:
        conn.close()
    started = time.monotonic()
    out = tool_grep_corpus("rfcs", _EVIL, regex=True)
    assert time.monotonic() - started < 6.0
    assert "INCOMPLETE SCAN" in out
    assert "evidence of absence" not in out
    assert len(spawned) == 1 and _gone(spawned[0])


def test_soft_stop_between_files_reports_what_was_read(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow-but-finishing regex stops at the budget between files and says
    how far it got, rather than being killed with nothing to report."""
    monkeypatch.setattr(grep, "_REGEX_SCAN_BUDGET_S", -1.0)
    write_cache_file(isolated_home, "wg", "threads/a.md", "needle\n")
    write_cache_file(isolated_home, "wg", "threads/b.md", "needle\n")
    out = tool_grep_corpus("wg", "need.e", regex=True)
    assert "INCOMPLETE SCAN" in out
    assert "0 of 2 file(s)" in out


def test_regex_results_match_the_literal_path(isolated_home: Path) -> None:
    """Child and in-process scans share one code path: same counts, same
    caps, same eliding of a long line around its match."""
    write_cache_file(
        isolated_home, "wg", "threads/long.md", ("x" * 3000) + " needle here\n"
    )
    write_cache_file(isolated_home, "wg", "threads/many.md", "needle\n" * 30)
    for kwargs in ({}, {"max_per_file": 2}, {"limit": 5}, {"files_only": True}):
        literal = tool_grep_corpus("wg", "needle", **kwargs)
        via_child = tool_grep_corpus("wg", "need[l]e", regex=True, **kwargs)
        normalised = via_child.replace("need[l]e", "needle").replace(
            "regex, case-insensitive", "literal, case-insensitive"
        )
        assert normalised == literal, kwargs
        assert "needle here" in via_child or kwargs.get("files_only")


def test_child_failure_is_not_absence() -> None:
    with pytest.raises(regexscan.ScanFailed):
        regexscan.run_bounded(
            {"kind": "files", "pattern": "(", "flags": 0, "files": [], "keep": 1}, 5.0
        )


def test_scans_draw_from_their_own_small_pool(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isolated_home
    monkeypatch.setenv("IETF_LLM_SCAN_CONCURRENCY", "2")
    seen: dict[str, int] = {}
    holder: List[anyio.CapacityLimiter] = []

    def probe() -> str:
        seen["scan"] = holder[0].borrowed_tokens
        seen["default"] = holder[1].borrowed_tokens
        return "ok"

    async def go() -> str:
        holder.append(common._scan_limiter())
        holder.append(anyio.to_thread.current_default_thread_limiter())
        seen["total"] = int(holder[0].total_tokens)
        return await common._offload_scan(probe)

    assert asyncio.run(go()) == "ok"
    assert seen == {"scan": 1, "default": 0, "total": 2}


def test_a_killed_scan_returns_its_token(
    isolated_home: Path, tight: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker thread must be finished, not abandoned: the token comes back
    once the child is killed, so a flood of pathological calls queues instead of
    wedging the pool."""
    assert tight is None
    monkeypatch.setenv("IETF_LLM_SCAN_CONCURRENCY", "1")
    write_cache_file(isolated_home, "wg", "threads/evil.md", _EVIL_TEXT)
    limiters: List[anyio.CapacityLimiter] = []

    async def go() -> List[str]:
        limiters.append(common._scan_limiter())
        # Two calls through a one-token pool: the second waits for the first.
        return list(
            await asyncio.gather(
                *(
                    common._offload_scan(
                        tool_grep_corpus, "wg", _EVIL, regex=True
                    )
                    for _ in range(2)
                )
            )
        )

    started = time.monotonic()
    outs = asyncio.run(go())
    assert all("INCOMPLETE SCAN" in out for out in outs)
    assert time.monotonic() - started < 12.0
    assert limiters[0].borrowed_tokens == 0
