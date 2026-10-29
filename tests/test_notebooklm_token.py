"""The NotebookLM OAuth token file holds a refresh token, so it must be
owner-only whether the file is new or an earlier run left it world-readable.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("google_auth_oauthlib")

from ietf_llm import notebooklm  # noqa: E402  pylint: disable=wrong-import-position


class _Creds:
    valid = True

    def to_json(self) -> str:
        return '{"refresh_token": "secret"}'


class _Flow:
    def run_local_server(self, port: int = 0) -> _Creds:
        return _Creds()


def _get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    secrets = tmp_path / "client_secrets.json"
    secrets.write_text("{}", encoding="utf-8")
    token = tmp_path / "token.json"
    monkeypatch.setattr(
        notebooklm.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda _cls, *_a, **_k: _Flow()),
    )
    assert notebooklm.get_credentials(str(secrets), str(token)) is not None
    return token


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_new_token_file_is_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old: Any = os.umask(0o022)
    try:
        token = _get(tmp_path, monkeypatch)
    finally:
        os.umask(old)
    assert _mode(token) == 0o600
    assert "secret" in token.read_text(encoding="utf-8")


def test_existing_world_readable_token_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "token.json").write_text("stale", encoding="utf-8")
    os.chmod(tmp_path / "token.json", 0o644)

    def _unreadable(*_a: Any, **_k: Any) -> Any:
        raise ValueError("bad")

    # An unreadable stored token forces the flow, which rewrites the file.
    monkeypatch.setattr(
        notebooklm.Credentials, "from_authorized_user_file", _unreadable
    )
    token = _get(tmp_path, monkeypatch)
    assert _mode(token) == 0o600
