"""The RFC text mirror and its reconciliation against the index (#230).

The reconciliation is the guard against RFC reissues: a republished RFC keeps
its number, so a join on `(rfc, off, len)` still succeeds and silently
attaches one document's text to another's vectors. What is asserted here is
that a changed byte is noticed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any, Dict, List

import pytest

from ietf_llm.log import Verbosity
from ietf_llm.rfcindex import mirror
from ietf_llm.rfcindex.format import RfcIndexError


def _write(root: str, rfc: str, body: bytes) -> str:
    os.makedirs(root, exist_ok=True)
    path = mirror.text_path(root, rfc)
    with open(path, "wb") as fh:
        fh.write(body)
    return hashlib.sha256(body).hexdigest()


def test_identical_mirror_reconciles_clean(tmp_path: Any) -> None:
    root = str(tmp_path / "m")
    digests = {
        "9110": _write(root, "9110", b"HTTP Semantics\n"),
        "17a": _write(root, "17a", b"an oddball\n"),
    }
    result = mirror.reconcile(root, digests)
    assert result.matched == 2
    assert result.usable
    assert "2/2 RFCs match" in result.summary()


def test_a_reissued_rfc_is_caught(tmp_path: Any) -> None:
    root = str(tmp_path / "m")
    digests = {"9110": _write(root, "9110", b"HTTP Semantics\n")}
    # Same number, same name, one byte different -- the reissue case.
    _write(root, "9110", b"HTTP Semantics!\n")
    result = mirror.reconcile(root, digests)
    assert result.matched == 0
    assert result.differing == ["9110"]
    assert not result.usable
    assert "1 differ" in result.summary()


def test_absent_and_differing_are_distinguished(tmp_path: Any) -> None:
    root = str(tmp_path / "m")
    digests = {"1": _write(root, "1", b"one\n"), "2": "0" * 64}
    _write(root, "2", b"two\n")
    digests["3"] = "f" * 64  # never written
    result = mirror.reconcile(root, digests)
    assert result.matched == 1
    assert result.differing == ["2"]
    assert result.absent == ["3"]
    assert result.total == 3


def test_no_digests_means_unverifiable_not_zero(tmp_path: Any) -> None:
    """An index predating sources.json is a real early-release case; claiming
    nothing matched would be a lie in the other direction."""
    result = mirror.reconcile(str(tmp_path), {})
    assert result.matched == 0
    assert result.usable
    assert result.total == 0


def test_sync_invokes_rsync_with_the_build_s_filter(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: List[List[str]] = []

    class _Done:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(mirror.shutil, "which", lambda _n: "/usr/bin/rsync")
    monkeypatch.setattr(
        mirror.subprocess,
        "run",
        lambda argv, **kw: (seen.append(argv), _Done())[1],
    )
    mirror.sync_mirror(str(tmp_path / "m"), verbosity=Verbosity.QUIET)
    argv = seen[0]
    assert argv[0] == "/usr/bin/rsync"
    assert "--include=rfc[0-9]*.txt" in argv and "--exclude=*" in argv
    # No --delete: a withdrawn file must not cost us a mirror an older index
    # still describes.
    assert "--delete" not in argv
    assert argv[-2].endswith("rfcs-text-only/")


def test_missing_rsync_is_a_clear_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mirror.shutil, "which", lambda _n: None)
    with pytest.raises(RfcIndexError, match="rsync is not on PATH"):
        mirror.sync_mirror(str(tmp_path / "m"), verbosity=Verbosity.QUIET)


def test_rsync_failure_is_not_swallowed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Failed:
        returncode = 23
        stderr = "rsync: connection unexpectedly closed\n"

    monkeypatch.setattr(mirror.shutil, "which", lambda _n: "/usr/bin/rsync")
    monkeypatch.setattr(mirror.subprocess, "run", lambda argv, **kw: _Failed())
    with pytest.raises(RfcIndexError, match="exited 23"):
        mirror.sync_mirror(str(tmp_path / "m"), verbosity=Verbosity.QUIET)


def test_rsync_timeout_is_an_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(argv: List[str], **_kw: Dict[str, Any]) -> None:
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(mirror.shutil, "which", lambda _n: "/usr/bin/rsync")
    monkeypatch.setattr(mirror.subprocess, "run", _boom)
    with pytest.raises(RfcIndexError, match="failed"):
        mirror.sync_mirror(str(tmp_path / "m"), verbosity=Verbosity.QUIET)
