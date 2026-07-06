"""Tests for the remote OpenAI-compatible summariser backend.

No network: oai_compat.requests.post is stubbed. Verifies prefix
detection, the chat-completions request/response surface, env-driven
construction, 429 retry, and that _Summarizer routes an
'openai-summarize/' model id to this backend.
"""

from __future__ import annotations

import requests

from ietf_llm.embeddings import oai_compat
from ietf_llm.digest.remote_summarizer import (
    _OpenAICompatChatModel,
    is_remote_summarize_model,
    load_openai_compat_chat,
)
from ietf_llm.digest.summarizer import _Summarizer
from ietf_llm.utils import Verbosity


class _FakeResp:
    def __init__(self, status, content="", headers=None):
        self.status_code = status
        self._content = content
        self.headers = headers or {}

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _chat(**kw):
    opts = dict(timeout=5.0, max_retries=3)
    opts.update(kw)
    return _OpenAICompatChatModel("m", "https://host/v1", {}, **opts)


def test_prefix_detection():
    assert is_remote_summarize_model("openai-summarize/gpt-4o-mini")
    assert not is_remote_summarize_model("gpt-4o-mini")


def test_prompt_request_and_response(monkeypatch):
    seen = {}

    def fake(url, headers, json, timeout):
        seen.update(url=url, headers=headers, payload=json)
        return _FakeResp(200, "a summary")

    monkeypatch.setattr(oai_compat.requests, "post", fake)
    out = _OpenAICompatChatModel(
        "@cf/meta/llama-3.1-8b-instruct", "https://host/v1",
        {"Authorization": "Bearer tok", "cf-aig-authorization": "g"},
        timeout=5.0, max_retries=0,
    ).prompt("summarise this").text()
    assert out == "a summary"
    assert seen["url"] == "https://host/v1/chat/completions"
    assert seen["payload"]["model"] == "@cf/meta/llama-3.1-8b-instruct"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "summarise this"}]
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"]["cf-aig-authorization"] == "g"


def test_prompt_handles_empty_choices(monkeypatch):
    class _Empty(_FakeResp):
        def json(self):
            return {"choices": []}

    monkeypatch.setattr(oai_compat.requests, "post",
                        lambda url, headers, json, timeout: _Empty(200))
    assert _chat(max_retries=0).prompt("x").text() == ""


def test_retry_then_succeed(monkeypatch):
    monkeypatch.setattr(oai_compat.time, "sleep", lambda *a, **k: None)
    n = {"i": 0}

    def fake(url, headers, json, timeout):
        n["i"] += 1
        return _FakeResp(429) if n["i"] <= 2 else _FakeResp(200, "ok")

    monkeypatch.setattr(oai_compat.requests, "post", fake)
    assert _chat(max_retries=3).prompt("x").text() == "ok"
    assert n["i"] == 3


def test_load_requires_base_url(monkeypatch):
    monkeypatch.delenv("IETF_LLM_SUMMARIZE_BASE_URL", raising=False)
    assert load_openai_compat_chat("openai-summarize/m", Verbosity.QUIET) is None


def test_load_builds_from_env(monkeypatch):
    monkeypatch.setenv("IETF_LLM_SUMMARIZE_BASE_URL", "https://host/v1")
    monkeypatch.setenv("IETF_LLM_SUMMARIZE_TOKEN", "tok")
    monkeypatch.setenv("IETF_LLM_SUMMARIZE_HEADERS", '{"cf-aig-authorization": "g"}')
    m = load_openai_compat_chat(
        "openai-summarize/@cf/meta/llama-3.1-8b-instruct", Verbosity.QUIET)
    assert isinstance(m, _OpenAICompatChatModel)
    assert m._url == "https://host/v1/chat/completions"
    assert m._model_id == "@cf/meta/llama-3.1-8b-instruct"
    assert m._headers["Authorization"] == "Bearer tok"
    assert m._headers["cf-aig-authorization"] == "g"


def test_summarizer_routes_to_remote(monkeypatch):
    monkeypatch.setenv("IETF_LLM_SUMMARIZE_BASE_URL", "https://host/v1")
    monkeypatch.delenv("IETF_LLM_SUMMARIZE_TOKEN", raising=False)
    monkeypatch.delenv("IETF_LLM_SUMMARIZE_HEADERS", raising=False)
    monkeypatch.setattr(oai_compat.requests, "post",
                        lambda url, headers, json, timeout: _FakeResp(200, '"Quoted summary."'))
    s = _Summarizer("openai-summarize/m", Verbosity.QUIET)
    assert s.active()
    # _Summarizer collapses newlines and strips surrounding quotes.
    assert s.summarize("text") == "Quoted summary."
