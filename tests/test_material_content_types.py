"""Agenda / minutes materials that are not text are pointed at, not decoded.

A PDF-uploaded agenda is served as `application/pdf`; `requests` would decode
its bytes as text and the mojibake would be written into `agenda.md`, embedded
and rev-stamped. The tests stub `fetch_resource` (not `_fetch_text`) so the
content-type handling is part of what is driven.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

import ietf_llm.gather.sources.meetings as meetings
from ietf_llm.gather.sources.meetings import MeetingCluster, _fetch_text, _process_cluster
from ietf_llm.log import Verbosity
from ietf_llm.paths import agenda_path, get_wg_file_cache_dir

WG = "httpbis"
CODE = "ietf104"
PDF_URL = "https://dt/meeting/104/materials/agenda-104-httpbis"
PDF_BODY = "%PDF-1.4 ��\x00binary"


def _response(ctype: str, text: str) -> Any:
    return SimpleNamespace(headers={"Content-Type": ctype}, text=text)


def _stub(monkeypatch: pytest.MonkeyPatch, ctype: str, text: str) -> None:
    monkeypatch.setattr(
        meetings, "fetch_resource", lambda _url, headers=None: _response(ctype, text)
    )


def test_pdf_becomes_a_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "application/pdf", PDF_BODY)
    got = _fetch_text(PDF_URL, Verbosity.QUIET)
    assert got and "application/pdf" in got and "%PDF" not in got


@pytest.mark.parametrize(
    "ctype", ["text/markdown; charset=UTF-8", "text/plain", "TEXT/PLAIN;charset=x"]
)
def test_text_types_pass_through(monkeypatch: pytest.MonkeyPatch, ctype: str) -> None:
    _stub(monkeypatch, ctype, "# Agenda\n\n1. Welcome\n")
    assert _fetch_text(PDF_URL, Verbosity.QUIET) == "# Agenda\n\n1. Welcome\n"


def test_html_is_still_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, "text/html; charset=utf-8", "<html><body><p>Item one</p></body></html>")
    got = _fetch_text(PDF_URL, Verbosity.QUIET)
    assert got and "Item one" in got and "<p>" not in got


def test_pdf_agenda_is_written_as_a_pointer_and_counted_fetched(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub(monkeypatch, "application/pdf", PDF_BODY)
    cluster = MeetingCluster(
        code=CODE,
        start=datetime(2019, 3, 25),
        end=datetime(2019, 3, 25),
        sessions=[
            {
                "number": "IETF 104",
                "date": "2019-03-25",
                "links": [
                    {"type": "agenda", "url": PDF_URL, "docname": "agenda-104-httpbis"}
                ],
            }
        ],
    )
    manifest: Dict[str, str] = {}
    _process_cluster(
        cluster,
        WG,
        get_wg_file_cache_dir(WG),
        Verbosity.QUIET,
        {"agenda-104-httpbis": "00"},
        manifest,
    )
    text = Path(agenda_path(get_wg_file_cache_dir(WG), CODE)).read_text(encoding="utf-8")
    assert f"URL: {PDF_URL}" in text
    assert "Non-text material (application/pdf)" in text
    assert "%PDF" not in text and "�" not in text
    # Counted as fetched, so the PDF is not downloaded again next gather.
    assert manifest["agenda-104-httpbis"] == "00"
