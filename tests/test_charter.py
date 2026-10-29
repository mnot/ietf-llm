"""`process_charter`: the charter file holds the approved text, not a proposal."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from ietf_llm.gather.sources import charter
from ietf_llm.log import Verbosity

APPROVED = ["/api/v1/doc/state/88/"]
RECHARTER = ["/api/v1/doc/state/90/"]


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc: Optional[Dict[str, Any]]
) -> Tuple[str, List[str]]:
    requested: List[str] = []

    def fake_fetch(url: str) -> Any:
        requested.append(url)
        return SimpleNamespace(text="CHARTER BODY")

    monkeypatch.setattr(charter, "get_group_type", lambda _wg: "ietf")
    monkeypatch.setattr(charter, "_get_json", lambda _path: doc)
    monkeypatch.setattr(charter, "fetch_resource", fake_fetch)
    out = tmp_path / "charter.txt"
    charter.process_charter("masque", str(out), Verbosity.QUIET)
    return (out.read_text() if out.exists() else ""), requested


def test_recharter_writes_the_approved_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, requested = _run(monkeypatch, tmp_path, {"rev": "03-02", "states": RECHARTER})
    assert requested == ["https://www.ietf.org/charter/charter-ietf-masque-03.txt"]
    assert "Source: https://www.ietf.org/charter/charter-ietf-masque-03.txt" in text
    assert (
        "Rechartering in progress: proposed revision 03-02 at "
        "https://www.ietf.org/charter/charter-ietf-masque-03-02.txt"
    ) in text
    assert "CHARTER BODY" in text


def test_approved_charter_has_no_recharter_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, requested = _run(monkeypatch, tmp_path, {"rev": "03", "states": APPROVED})
    assert requested == ["https://www.ietf.org/charter/charter-ietf-masque-03.txt"]
    assert "Rechartering" not in text


def test_never_approved_charter_is_labelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, requested = _run(monkeypatch, tmp_path, {"rev": "00-03", "states": RECHARTER})
    assert requested == ["https://www.ietf.org/charter/charter-ietf-masque-00-03.txt"]
    assert "never approved" in text
    assert "Rechartering" not in text


def test_no_charter_document_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text, requested = _run(monkeypatch, tmp_path, None)
    assert not text and not requested
