"""Tests for the OpenAI-compatible remote embedding backend.

No network: requests.Session.post is stubbed (the backend reuses one
keep-alive session, so that — not the module-level requests.post — is the
seam). Verifies the embed / embed_multi surface, input-order preservation
when the server reorders, batching to the configured size, and 429 / 5xx
retry-then-succeed, plus that _load_openai_compat reads endpoint + header-map
config from the env and self-disables when the base URL is missing.
"""

from __future__ import annotations

import pytest
import requests

from ietf_llm import oai_compat
from ietf_llm.embeddings import models
from ietf_llm.embeddings.models import (
    _OpenAICompatEmbeddingModel,
    _load_openai_compat,
)
from ietf_llm.utils import Verbosity


class _FakeResp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data if data is not None else []
        self.headers = headers or {}

    def json(self):
        return {"data": self._data}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _echo(inputs, reverse=False):
    # One row per input; embedding[0] encodes input length so a test can
    # tell which vector came from which input.
    rows = [{"index": i, "embedding": [float(len(s)), 1.0]}
            for i, s in enumerate(inputs)]
    return list(reversed(rows)) if reverse else rows


def _model(**kw):
    opts = dict(batch_size=96, timeout=5.0, max_retries=3)
    opts.update(kw)
    return _OpenAICompatEmbeddingModel("m", "https://host/v1", {}, **opts)


def test_embed_single(monkeypatch):
    monkeypatch.setattr(requests.Session, "post",
                        lambda self, url, headers, json, timeout: _FakeResp(200, _echo(json["input"])))
    assert _model().embed("hello") == [5.0, 1.0]


def test_embed_multi_preserves_input_order(monkeypatch):
    # Server returns rows in reversed index order; backend must reorder.
    monkeypatch.setattr(requests.Session, "post",
                        lambda self, url, headers, json, timeout: _FakeResp(200, _echo(json["input"], reverse=True)))
    out = _model().embed_multi(["a", "bb", "ccc"])
    assert [v[0] for v in out] == [1.0, 2.0, 3.0]


def test_embed_multi_raises_on_short_response(monkeypatch):
    # A server returning fewer vectors than inputs would misalign every
    # chunk<->vector pair the caller zips; the backend must fail loudly.
    monkeypatch.setattr(requests.Session, "post",
                        lambda self, url, headers, json, timeout: _FakeResp(200, _echo(json["input"])[:-1]))
    with pytest.raises(ValueError):
        _model().embed_multi(["a", "bb", "ccc"])


def test_embed_multi_raises_on_duplicate_index(monkeypatch):
    # Two rows claiming the same index leave another input with no vector;
    # silently accepting it would shift the alignment.
    def dup(self, url, headers, json, timeout):
        rows = _echo(json["input"])
        rows[1]["index"] = 0
        return _FakeResp(200, rows)

    monkeypatch.setattr(requests.Session, "post", dup)
    with pytest.raises(ValueError):
        _model().embed_multi(["a", "bb", "ccc"])


def test_embed_multi_batches_to_configured_size(monkeypatch):
    calls = []

    def fake(self, url, headers, json, timeout):
        calls.append(len(json["input"]))
        return _FakeResp(200, _echo(json["input"]))

    monkeypatch.setattr(requests.Session, "post", fake)
    out = _model(batch_size=2).embed_multi(["a", "b", "c", "d", "e"])
    assert calls == [2, 2, 1]
    assert len(out) == 5


def test_reuses_one_session_across_batches(monkeypatch):
    # Connection reuse is the point: every batch goes through the model's one
    # keep-alive session, not a fresh connection per call.
    sessions = []

    def fake(self, url, headers, json, timeout):
        sessions.append(self)
        return _FakeResp(200, _echo(json["input"]))

    monkeypatch.setattr(requests.Session, "post", fake)
    m = _model(batch_size=1)
    m.embed_multi(["a", "b", "c"])
    assert len(sessions) == 3
    assert all(s is m._session for s in sessions)


def test_url_model_id_and_header_map_sent(monkeypatch):
    seen = {}

    def fake(self, url, headers, json, timeout):
        seen.update(url=url, headers=headers, model=json["model"])
        return _FakeResp(200, _echo(json["input"]))

    monkeypatch.setattr(requests.Session, "post", fake)
    _OpenAICompatEmbeddingModel(
        "@cf/baai/bge-small-en-v1.5", "https://host/v1",
        {"Authorization": "Bearer tok", "cf-aig-authorization": "g"},
        batch_size=96, timeout=5.0, max_retries=0,
    ).embed("q")
    assert seen["url"] == "https://host/v1/embeddings"
    assert seen["model"] == "@cf/baai/bge-small-en-v1.5"
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"]["cf-aig-authorization"] == "g"


@pytest.mark.parametrize("status", [429, 503])
def test_retry_then_succeed(monkeypatch, status):
    monkeypatch.setattr(oai_compat.time, "sleep", lambda *a, **k: None)
    n = {"i": 0}

    def fake(self, url, headers, json, timeout):
        n["i"] += 1
        return _FakeResp(status) if n["i"] <= 2 else _FakeResp(200, _echo(json["input"]))

    monkeypatch.setattr(requests.Session, "post", fake)
    assert _model(max_retries=3).embed("hi") == [2.0, 1.0]
    assert n["i"] == 3


def test_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr(oai_compat.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(requests.Session, "post",
                        lambda self, url, headers, json, timeout: _FakeResp(500))
    with pytest.raises(requests.HTTPError):
        _model(max_retries=1).embed("hi")


def test_load_requires_base_url(monkeypatch):
    monkeypatch.delenv("IETF_LLM_EMBED_BASE_URL", raising=False)
    assert _load_openai_compat("openai-embed/m", Verbosity.QUIET) is None


def test_load_builds_from_env(monkeypatch):
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    monkeypatch.setenv("IETF_LLM_EMBED_TOKEN", "tok")
    monkeypatch.setenv("IETF_LLM_EMBED_HEADERS", '{"cf-aig-authorization": "g"}')
    m = _load_openai_compat("openai-embed/@cf/baai/bge-small-en-v1.5", Verbosity.QUIET)
    assert isinstance(m, _OpenAICompatEmbeddingModel)
    assert m._model_id == "@cf/baai/bge-small-en-v1.5"
    assert m._headers["Authorization"] == "Bearer tok"
    assert m._headers["cf-aig-authorization"] == "g"


def test_load_bad_headers_json_ignored(monkeypatch):
    monkeypatch.setenv("IETF_LLM_EMBED_BASE_URL", "https://host/v1")
    monkeypatch.setenv("IETF_LLM_EMBED_HEADERS", "not-json")
    m = _load_openai_compat("openai-embed/m", Verbosity.QUIET)
    assert isinstance(m, _OpenAICompatEmbeddingModel)
    assert "Authorization" not in m._headers

