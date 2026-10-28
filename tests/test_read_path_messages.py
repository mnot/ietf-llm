"""The in-flight refresh banner says what is true for the backend in use.

On the local backend a gather writes into the live cache, so files change in
place between calls; on the cloud backend readers see the last published
version. Nothing here reads the real cache.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from ietf_llm import mcp
from ietf_llm.gather import runner as gather_runner


def _running(monkeypatch: pytest.MonkeyPatch) -> None:
    status: Dict[str, Any] = {
        "state": "running", "stage_index": 5, "stage_total": 19, "stage": "mail",
    }
    monkeypatch.setattr(gather_runner, "local_inflight", lambda wg: dict(status))


def test_banner_local_says_files_change_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "local")
    _running(monkeypatch)
    note = mcp.common._inflight_refresh_note("wg")
    assert note is not None
    assert "\n" not in note
    assert "stage 5/19" in note
    assert "in place" in note
    assert "counts and file lists can change" in note
    assert "previous index" in note
    assert 'gather_status(corpus="wg")' in note
    assert "previous* gather" not in note  # the cloud claim must not leak here


def test_banner_cloud_keeps_previous_version_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    _running(monkeypatch)
    note = mcp.common._inflight_refresh_note("wg")
    assert note is not None
    assert "\n" not in note
    assert "stage 5/19" in note
    assert "*previous* gather" in note
    assert "in place" not in note
    assert 'gather_status(corpus="wg")' in note


def test_banner_absent_without_a_gather(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gather_runner, "local_inflight", lambda wg: None)
    assert mcp.common._inflight_refresh_note("wg") is None


def test_session_guidance_follows_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "local")
    assert "in place" in mcp.common._regather_read_clause()
    monkeypatch.setenv("IETF_LLM_STORE_BACKEND", "cloud")
    assert "last published version" in mcp.common._regather_read_clause()
