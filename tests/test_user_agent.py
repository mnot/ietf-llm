"""Every outbound fetch must identify the client.

`net.transport.DEFAULT_HEADERS` carries the `User-Agent` that gives upstream
operators a contact path (the repo URL) instead of a bare `python-requests` /
`Python-urllib` string to blind-block. Two call sites had drifted off it: the
mailarchive list-existence probe in `gather.sources.mbox`, and the seed-store
consumer in `seed.fetch`, which is on urllib rather than the shared session.
These pin both back.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from ietf_llm.net import DEFAULT_HEADERS
from ietf_llm.seed import fetch


def test_default_user_agent_names_the_project_and_a_contact_url() -> None:
    ua = DEFAULT_HEADERS["User-Agent"]
    assert ua.startswith("ietf-llm/")
    assert "+https://github.com/mnot/ietf-llm" in ua


class _StatusResp:
    status_code = 200


def test_mailarchive_probe_sends_the_user_agent(
    isolated_home: Path, monkeypatch: Any,
) -> None:
    from ietf_llm.gather.sources import mbox  # pylint: disable=import-outside-toplevel
    from ietf_llm.log import Verbosity

    seen: List[Dict[str, str]] = []

    def fake_get(url: str, **kwargs: Any) -> _StatusResp:
        seen.append(kwargs.get("headers") or {})
        return _StatusResp()

    monkeypatch.setattr("ietf_llm.gather.sources.mbox.governed_get", fake_get)
    mbox.validate_list_names(["httpbis@ietf.org"], verbose=Verbosity.QUIET)
    assert seen and all(
        h.get("User-Agent") == DEFAULT_HEADERS["User-Agent"] for h in seen
    )


class _FakeUrlopen:
    """Stand in for `urllib.request.urlopen`, recording the Request it was given."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.requests: List[urllib.request.Request] = []

    def __call__(self, req: Any, **_kwargs: Any) -> Any:
        self.requests.append(req)
        return self

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self, *_args: Any) -> bytes:
        body, self.body = self.body, b""
        return body

    @property
    def user_agents(self) -> List[Any]:
        # urllib capitalises header names on Request.add_header.
        return [r.get_header("User-agent") for r in self.requests]


def test_seed_read_bytes_sends_the_user_agent(monkeypatch: Any) -> None:
    stub = _FakeUrlopen(b'{"format": 1}')
    monkeypatch.setattr(urllib.request, "urlopen", stub)
    assert fetch._read_bytes("https://seed.example/index.json") == b'{"format": 1}'
    assert stub.user_agents == [DEFAULT_HEADERS["User-Agent"]]


def test_seed_download_sends_the_user_agent(monkeypatch: Any, tmp_path: Path) -> None:
    stub = _FakeUrlopen(b"bundle-bytes")
    monkeypatch.setattr(urllib.request, "urlopen", stub)
    dest = tmp_path / "bundle.tar.gz"
    fetch._download("https://seed.example/b.tar.gz", str(dest))
    assert dest.read_bytes() == b"bundle-bytes"
    assert stub.user_agents == [DEFAULT_HEADERS["User-Agent"]]


def test_seed_local_path_is_unaffected(tmp_path: Path) -> None:
    # A filesystem seed base never builds a Request; keep that path working.
    src = tmp_path / "index.json"
    src.write_bytes(b"local")
    assert fetch._read_bytes(str(src)) == b"local"
    dest = tmp_path / "copy.json"
    fetch._download(str(src), str(dest))
    assert dest.read_bytes() == b"local"
